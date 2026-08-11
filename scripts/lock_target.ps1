[CmdletBinding()]
param(
    [string]$RepoPath = '',
    [string]$GitExecutable = '',
    [switch]$VerifyOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ProjectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
if ([string]::IsNullOrWhiteSpace($RepoPath)) {
    $RepoPath = Join-Path $ProjectRoot '.target-src\userland'
}
if ([string]::IsNullOrWhiteSpace($GitExecutable)) {
    $gitCommand = Get-Command git -ErrorAction SilentlyContinue
    if ($null -ne $gitCommand) {
        $GitExecutable = $gitCommand.Source
    }
    elseif (Test-Path -LiteralPath 'C:\Program Files\Git\cmd\git.exe' -PathType Leaf) {
        $GitExecutable = 'C:\Program Files\Git\cmd\git.exe'
    }
    else {
        throw 'Git executable was not found. Pass -GitExecutable with an explicit path.'
    }
}
$ResolvedRepo = [System.IO.Path]::GetFullPath($RepoPath)
$SafeRepo = $ResolvedRepo.Replace('\', '/')

$ExpectedRepositoryUrl = 'https://github.com/raspberrypi/userland.git'
$ExpectedRepositoryWebUrl = 'https://github.com/raspberrypi/userland'
$ExpectedCommit = 'a54a0dbb2b8dcf9bafdddfc9a9374fb51d97e976'
$ExpectedCommitDate = '2024-12-23'
$IncludePatterns = @('**/*.c', '**/*.h', '**/*.cpp')
$IncludeExtensions = @('.c', '.h', '.cpp')

function Invoke-TargetGit {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $output = @(& $GitExecutable -c "safe.directory=$SafeRepo" -c 'core.quotepath=false' -C $ResolvedRepo @Arguments 2>&1)
    if ($LASTEXITCODE -ne 0) {
        $detail = ($output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
        throw "Git command failed: git $($Arguments -join ' ')`n$detail"
    }
    return @($output | ForEach-Object { $_.ToString() })
}

function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)

    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($sha.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

function Get-FileListHash {
    param([Parameter(Mandatory = $true)][string[]]$Paths)

    $utf8 = New-Object System.Text.UTF8Encoding($false)
    $stream = New-Object System.IO.MemoryStream
    try {
        $prefix = $utf8.GetBytes("ai-sast-target-file-list-v1`0")
        $stream.Write($prefix, 0, $prefix.Length)
        foreach ($path in $Paths) {
            $bytes = $utf8.GetBytes($path)
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.WriteByte(0)
        }
        return Get-Sha256Hex -Bytes $stream.ToArray()
    }
    finally {
        $stream.Dispose()
    }
}

function Get-PrefixedTextHash {
    param(
        [Parameter(Mandatory = $true)][string]$Prefix,
        [Parameter(Mandatory = $true)][string]$Text
    )

    $utf8 = New-Object System.Text.UTF8Encoding($false)
    return Get-Sha256Hex -Bytes $utf8.GetBytes($Prefix + [char]0 + $Text)
}

function ConvertTo-StableJson {
    param(
        [Parameter(Mandatory = $true)]$Value,
        [switch]$Compress
    )

    $json = $Value | ConvertTo-Json -Depth 20 -Compress
    if ($Compress) {
        return $json
    }

    $builder = New-Object System.Text.StringBuilder
    $indent = 0
    $inString = $false
    $escaped = $false
    for ($index = 0; $index -lt $json.Length; $index += 1) {
        $character = $json[$index]
        if ($inString) {
            [void]$builder.Append($character)
            if ($escaped) {
                $escaped = $false
            }
            elseif ($character -eq '\') {
                $escaped = $true
            }
            elseif ($character -eq '"') {
                $inString = $false
            }
            continue
        }

        if ($character -eq '"') {
            $inString = $true
            [void]$builder.Append($character)
            continue
        }

        switch ($character) {
            { $_ -eq '{' -or $_ -eq '[' } {
                $closing = if ($character -eq '{') { '}' } else { ']' }
                if (($index + 1) -lt $json.Length -and $json[$index + 1] -eq $closing) {
                    [void]$builder.Append($character)
                    [void]$builder.Append($closing)
                    $index += 1
                }
                else {
                    [void]$builder.Append($character)
                    [void]$builder.Append("`n")
                    $indent += 1
                    [void]$builder.Append(('  ' * $indent))
                }
            }
            { $_ -eq '}' -or $_ -eq ']' } {
                [void]$builder.Append("`n")
                $indent -= 1
                [void]$builder.Append(('  ' * $indent))
                [void]$builder.Append($character)
            }
            ',' {
                [void]$builder.Append(',')
                [void]$builder.Append("`n")
                [void]$builder.Append(('  ' * $indent))
            }
            ':' {
                [void]$builder.Append(': ')
            }
            default {
                if (-not [char]::IsWhiteSpace($character)) {
                    [void]$builder.Append($character)
                }
            }
        }
    }
    return $builder.ToString()
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )

    $parent = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $utf8)
}

function Test-ByteArraysEqual {
    param(
        [Parameter(Mandatory = $true)][byte[]]$Left,
        [Parameter(Mandatory = $true)][byte[]]$Right
    )

    if ($Left.Length -ne $Right.Length) {
        return $false
    }
    for ($index = 0; $index -lt $Left.Length; $index += 1) {
        if ($Left[$index] -ne $Right[$index]) {
            return $false
        }
    }
    return $true
}

function Read-StrictUtf8NoBomLf {
    param([Parameter(Mandatory = $true)][string]$Path)

    $bytes = [System.IO.File]::ReadAllBytes($Path)
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        throw "UTF-8 BOM is not allowed: $Path"
    }
    if ($bytes -contains 0x0D) {
        throw "CR/CRLF line endings are not allowed: $Path"
    }
    $strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
    try {
        return $strictUtf8.GetString($bytes)
    }
    catch {
        throw "File is not valid UTF-8: $Path"
    }
}

function Get-OutOfScopeReason {
    param(
        [Parameter(Mandatory = $true)][string]$Mode,
        [AllowEmptyString()][string]$Extension
    )

    if ($Mode -eq '120000') {
        return 'symbolic link; links are not followed in the v1 target contract'
    }
    switch ($Extension) {
        '.s' { return 'ARM assembly; outside the v1 C/C++ parser scope' }
        '.qasm' { return 'VideoCore QPU assembly; outside the v1 C/C++ parser scope' }
        '.qinc' { return 'VideoCore QPU assembly include; outside the v1 C/C++ parser scope' }
        '.in' { return 'build/config template; not a concrete C/C++ parser input' }
        default { return 'tracked non-C/C++ artifact' }
    }
}

function Get-ContractSnapshot {
    if (-not (Test-Path -LiteralPath $ResolvedRepo -PathType Container)) {
        throw "Target checkout does not exist: $ResolvedRepo"
    }
    if (-not (Test-Path -LiteralPath $GitExecutable -PathType Leaf)) {
        throw "Git executable does not exist: $GitExecutable"
    }

    $origin = ((Invoke-TargetGit -Arguments @('remote', 'get-url', 'origin')) -join '').Trim()
    if ($origin -ne $ExpectedRepositoryUrl) {
        throw "Target origin mismatch. Expected '$ExpectedRepositoryUrl', got '$origin'."
    }

    $head = ((Invoke-TargetGit -Arguments @('rev-parse', 'HEAD')) -join '').Trim()
    if ($head -ne $ExpectedCommit) {
        throw "Target commit mismatch. Expected '$ExpectedCommit', got '$head'."
    }

    $objectType = ((Invoke-TargetGit -Arguments @('cat-file', '-t', $ExpectedCommit)) -join '').Trim()
    if ($objectType -ne 'commit') {
        throw "Locked object is not a commit: $ExpectedCommit"
    }

    $commitDate = ((Invoke-TargetGit -Arguments @('show', '-s', '--format=%cs', $ExpectedCommit)) -join '').Trim()
    if ($commitDate -ne $ExpectedCommitDate) {
        throw "Commit date mismatch. Expected '$ExpectedCommitDate', got '$commitDate'."
    }

    $status = @(Invoke-TargetGit -Arguments @('status', '--porcelain=v1', '--untracked-files=all'))
    if ($status.Count -ne 0) {
        throw "Target worktree must be clean. Drift:`n$($status -join [Environment]::NewLine)"
    }

    $treeLines = @(Invoke-TargetGit -Arguments @('ls-tree', '-r', '-l', '--full-tree', $ExpectedCommit))
    if ($treeLines.Count -eq 0) {
        throw 'Locked Git tree is empty.'
    }

    $extensionSet = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::Ordinal)
    foreach ($extension in $IncludeExtensions) {
        [void]$extensionSet.Add($extension)
    }

    $inventory = New-Object System.Collections.Generic.List[object]
    $sourceEntries = New-Object System.Collections.Generic.List[object]
    $excludedEntries = New-Object System.Collections.Generic.List[object]
    $seenPaths = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::Ordinal)
    $submoduleCount = 0

    foreach ($line in $treeLines) {
        if ($line -notmatch '^(\d{6})\s+(\w+)\s+([0-9a-f]+)\s+(-|\d+)\t(.*)$') {
            throw "Unsupported git ls-tree record: $line"
        }

        $mode = $Matches[1]
        $type = $Matches[2]
        $oid = $Matches[3]
        $sizeText = $Matches[4]
        $path = $Matches[5].Replace('\', '/')

        if (-not $seenPaths.Add($path)) {
            throw "Duplicate tracked path: $path"
        }
        if ($path.IndexOf([char]0) -ge 0) {
            throw "NUL is not allowed in a manifest path: $path"
        }

        if ($mode -eq '160000' -or $type -eq 'commit') {
            $submoduleCount += 1
        }

        $extension = [System.IO.Path]::GetExtension($path)
        $isRegularBlob = $type -eq 'blob' -and ($mode -eq '100644' -or $mode -eq '100755')
        $isInScope = $isRegularBlob -and $extensionSet.Contains($extension)
        $sizeBytes = if ($sizeText -eq '-') { $null } else { [int64]$sizeText }
        $classification = if ($isInScope) { 'IN_SCOPE' } else { 'OUT_OF_SCOPE' }
        $reason = if ($isInScope) { 'tracked C/C++ source or header; no path exclusion applies' } else { Get-OutOfScopeReason -Mode $mode -Extension $extension }

        $entry = [ordered]@{
            path = $path
            git_blob_oid = $oid
            mode = $mode
            size_bytes = $sizeBytes
            extension = $extension
            classification = $classification
            reason = $reason
        }
        $inventory.Add($entry)
        if ($isInScope) {
            $sourceEntries.Add($entry)
        }
        else {
            $excludedEntries.Add($entry)
        }
    }

    if ($submoduleCount -ne 0) {
        throw "Submodules are not supported by this contract; found $submoduleCount."
    }

    $inventory.Sort([System.Comparison[object]]{ param($a, $b) [System.StringComparer]::Ordinal.Compare($a.path, $b.path) })
    $sourceEntries.Sort([System.Comparison[object]]{ param($a, $b) [System.StringComparer]::Ordinal.Compare($a.path, $b.path) })
    $excludedEntries.Sort([System.Comparison[object]]{ param($a, $b) [System.StringComparer]::Ordinal.Compare($a.path, $b.path) })

    if ($inventory.Count -ne 830) {
        throw "Tracked inventory mismatch. Expected 830, got $($inventory.Count)."
    }
    if ($sourceEntries.Count -ne 654) {
        throw "In-scope inventory mismatch. Expected 654, got $($sourceEntries.Count)."
    }
    if ($excludedEntries.Count -ne 176) {
        throw "Out-of-scope inventory mismatch. Expected 176, got $($excludedEntries.Count)."
    }
    if (($sourceEntries.Count + $excludedEntries.Count) -ne $inventory.Count) {
        throw 'Every tracked path must be classified exactly once.'
    }

    $paths = [string[]]@($sourceEntries | ForEach-Object { [string]$_.path })
    [System.Array]::Sort($paths, [System.StringComparer]::Ordinal)
    $fileListHash = Get-FileListHash -Paths $paths

    $includeForHash = [string[]]$IncludePatterns.Clone()
    [System.Array]::Sort($includeForHash, [System.StringComparer]::Ordinal)
    $scopeHashInput = [ordered]@{
        allow_submodules = $false
        analysis_content_source = 'git_blob_at_locked_commit'
        build_reachability_filter = 'disabled'
        bundled_opensource = 'include'
        commit_sha = $ExpectedCommit
        examples = 'include'
        exclude = @()
        file_count = $sourceEntries.Count
        follow_symlinks = $false
        include = $includeForHash
        language_family = @('c', 'cpp')
        path_case_sensitive = $true
        precedence = 'exclude_wins'
        preprocessor_branch_filter = 'disabled'
        repository_url = $ExpectedRepositoryUrl
        require_clean_worktree = $true
        require_exact_commit = $true
        schema_version = 1
        source = 'git_tree'
        target_file_list_sha256 = $fileListHash
        tests = 'include'
        tracked_only = $true
    }
    $scopeCanonicalJson = ConvertTo-StableJson -Value $scopeHashInput -Compress
    $scopeHash = Get-PrefixedTextHash -Prefix 'ai-sast-scope-v1' -Text $scopeCanonicalJson

    $extensionCounts = [ordered]@{
        '.c' = @($sourceEntries | Where-Object { $_.extension -eq '.c' }).Count
        '.h' = @($sourceEntries | Where-Object { $_.extension -eq '.h' }).Count
        '.cpp' = @($sourceEntries | Where-Object { $_.extension -eq '.cpp' }).Count
    }
    if ($extensionCounts['.c'] -ne 284 -or $extensionCounts['.h'] -ne 367 -or $extensionCounts['.cpp'] -ne 3) {
        throw "Extension inventory mismatch: $((ConvertTo-StableJson -Value $extensionCounts -Compress))"
    }

    $topLevelRaw = @{}
    foreach ($entry in $sourceEntries) {
        $separator = $entry.path.IndexOf('/')
        $top = if ($separator -lt 0) { '(root)' } else { $entry.path.Substring(0, $separator) }
        if (-not $topLevelRaw.ContainsKey($top)) {
            $topLevelRaw[$top] = 0
        }
        $topLevelRaw[$top] += 1
    }
    $topKeys = [string[]]@($topLevelRaw.Keys)
    [System.Array]::Sort($topKeys, [System.StringComparer]::Ordinal)
    $topLevelCounts = [ordered]@{}
    foreach ($key in $topKeys) {
        $topLevelCounts[$key] = $topLevelRaw[$key]
    }

    $lock = [ordered]@{
        schema_version = 1
        target = [ordered]@{
            id = 'raspberrypi-userland'
            display_name = 'Raspberry Pi Userland'
            repository_url = $ExpectedRepositoryUrl
            repository_web_url = $ExpectedRepositoryWebUrl
            ref_at_lock = 'refs/heads/master'
            commit_sha = $ExpectedCommit
            commit_date = $ExpectedCommitDate
            license = 'BSD-3-Clause'
            submodules = @()
        }
        checkout = [ordered]@{
            source = 'git_tree'
            analysis_content_source = 'git_blob_at_locked_commit'
            tracked_only = $true
            require_exact_commit = $true
            require_clean_worktree = $true
            follow_symlinks = $false
            allow_submodules = $false
        }
        analysis_scope = [ordered]@{
            language_family = @('c', 'cpp')
            include_patterns = $IncludePatterns
            exclude_paths = @()
            precedence = 'exclude_wins'
            path_case_sensitive = $true
            tests = 'include'
            examples = 'include'
            bundled_opensource = 'include'
            build_reachability_filter = 'disabled'
            preprocessor_branch_filter = 'disabled'
            target_file_list_sha256 = $fileListHash
            scope_sha256 = $scopeHash
        }
        expected_inventory = [ordered]@{
            tracked_files = $inventory.Count
            in_scope_files = $sourceEntries.Count
            out_of_scope_files = $excludedEntries.Count
            in_scope_by_extension = $extensionCounts
            in_scope_by_top_level = $topLevelCounts
        }
        integrity = [ordered]@{
            fail_if_remote_mismatch = $true
            fail_if_commit_mismatch = $true
            fail_if_worktree_dirty = $true
            fail_if_inventory_mismatch = $true
            fail_if_duplicate_path = $true
            fail_if_unclassified_tracked_file = $true
            fail_if_in_scope_path_excluded = $true
        }
        artifacts = [ordered]@{
            target_inventory = 'artifacts/coverage/target_inventory.json'
            source_manifest = 'artifacts/coverage/source_manifest.jsonl'
            exclusion_manifest = 'artifacts/coverage/exclusion_manifest.jsonl'
            target_verification = 'artifacts/coverage/target_verification.json'
        }
        experiment_invalidation = [ordered]@{
            trigger = 'any_semantic_target_lock_change'
            action = 'create_new_experiment_id_and_rebuild_all_downstream_artifacts'
        }
    }

    $inventoryDocument = [ordered]@{
        schema_version = 1
        repository_url = $ExpectedRepositoryUrl
        commit_sha = $ExpectedCommit
        tracked_files = $inventory.Count
        in_scope_files = $sourceEntries.Count
        out_of_scope_files = $excludedEntries.Count
        target_file_list_sha256 = $fileListHash
        scope_sha256 = $scopeHash
        files = $inventory
    }

    $verification = [ordered]@{
        schema_version = 1
        status = 'PASS'
        repository_url = $ExpectedRepositoryUrl
        commit_sha = $ExpectedCommit
        scope_sha256 = $scopeHash
        scope_hash_canonical_json = $scopeCanonicalJson
        checks = [ordered]@{
            origin_matches = $true
            exact_commit = $true
            clean_worktree = $true
            submodule_count = 0
            tracked_files = $inventory.Count
            in_scope_files = $sourceEntries.Count
            out_of_scope_files = $excludedEntries.Count
            duplicate_paths = 0
            unclassified_paths = 0
            excluded_in_scope_paths = 0
        }
    }

    $lockText = (ConvertTo-StableJson -Value $lock) + "`n"
    $inventoryText = (ConvertTo-StableJson -Value $inventoryDocument) + "`n"
    $sourceText = (($sourceEntries | ForEach-Object { ConvertTo-StableJson -Value $_ -Compress }) -join "`n") + "`n"
    $excludedText = (($excludedEntries | ForEach-Object { ConvertTo-StableJson -Value $_ -Compress }) -join "`n") + "`n"
    $verificationText = (ConvertTo-StableJson -Value $verification) + "`n"

    return [ordered]@{
        lock = $lockText
        inventory = $inventoryText
        source_manifest = $sourceText
        exclusion_manifest = $excludedText
        verification = $verificationText
        file_count = $sourceEntries.Count
        target_file_list_sha256 = $fileListHash
        scope_sha256 = $scopeHash
    }
}

function Assert-NoPlaceholder {
    param([Parameter(Mandatory = $true)][string]$Text)

    $placeholderPattern = '(?i)\b(TODO|TBD|TBC|FIXME|CHANGEME|REPLACE_ME|PLACEHOLDER)\b|\$\{|\{\{|<[^>]+>|example\.com'
    if ($Text -match $placeholderPattern) {
        throw "LOCK_PLACEHOLDER: target.lock.yaml contains a placeholder-like value: '$($Matches[0])'."
    }
}

$snapshot = Get-ContractSnapshot
$paths = [ordered]@{
    lock = Join-Path $ProjectRoot 'target.lock.yaml'
    inventory = Join-Path $ProjectRoot 'artifacts\coverage\target_inventory.json'
    source_manifest = Join-Path $ProjectRoot 'artifacts\coverage\source_manifest.jsonl'
    exclusion_manifest = Join-Path $ProjectRoot 'artifacts\coverage\exclusion_manifest.jsonl'
    verification = Join-Path $ProjectRoot 'artifacts\coverage\target_verification.json'
}

Assert-NoPlaceholder -Text $snapshot.lock

if ($VerifyOnly) {
    foreach ($name in $paths.Keys) {
        $path = $paths[$name]
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Missing Phase 0 artifact: $path"
        }
        $actualBytes = [System.IO.File]::ReadAllBytes($path)
        $utf8 = New-Object System.Text.UTF8Encoding($false)
        $expectedBytes = $utf8.GetBytes([string]$snapshot[$name])
        if (-not (Test-ByteArraysEqual -Left $actualBytes -Right $expectedBytes)) {
            throw "Phase 0 artifact drift: $path. Regenerate with scripts/lock_target.ps1."
        }
    }

    $actualLock = $snapshot.lock | ConvertFrom-Json

    $schemaPath = Join-Path $ProjectRoot 'schemas\target-lock.schema.json'
    $profilePath = Join-Path $ProjectRoot 'docs\target_profile.md'
    if (-not (Test-Path -LiteralPath $schemaPath -PathType Leaf)) {
        throw "Missing target lock schema: $schemaPath"
    }
    if (-not (Test-Path -LiteralPath $profilePath -PathType Leaf)) {
        throw "Missing target profile: $profilePath"
    }

    $schemaText = Read-StrictUtf8NoBomLf -Path $schemaPath
    $profileText = Read-StrictUtf8NoBomLf -Path $profilePath
    Assert-NoPlaceholder -Text $schemaText
    Assert-NoPlaceholder -Text $profileText
    $schema = $schemaText | ConvertFrom-Json

    if ($schema.additionalProperties -ne $false) { throw 'Schema root must reject additional properties.' }
    if ($schema.properties.schema_version.const -ne 1) { throw 'Schema version contract drift.' }
    if ($schema.properties.target.properties.repository_url.const -cne $ExpectedRepositoryUrl) { throw 'Schema repository URL drift.' }
    if ($schema.properties.target.properties.commit_sha.pattern -cne '^[0-9a-f]{40}$') { throw 'Schema full commit SHA rule drift.' }
    if ($schema.properties.expected_inventory.properties.tracked_files.const -ne 830) { throw 'Schema tracked-file count drift.' }
    if ($schema.properties.expected_inventory.properties.in_scope_files.const -ne 654) { throw 'Schema in-scope count drift.' }
    if ($schema.properties.expected_inventory.properties.out_of_scope_files.const -ne 176) { throw 'Schema out-of-scope count drift.' }
    if ($schema.properties.analysis_scope.properties.exclude_paths.maxItems -ne 0) { throw 'Schema must forbid tracked C/C++ path exclusions.' }

    $profileRequiredValues = @(
        $ExpectedRepositoryUrl,
        $ExpectedCommit,
        '| `.c` | 284 |',
        '| `.h` | 367 |',
        '| `.cpp` | 3 |',
        $snapshot.target_file_list_sha256,
        $snapshot.scope_sha256
    )
    foreach ($requiredValue in $profileRequiredValues) {
        if ($profileText.IndexOf($requiredValue, [System.StringComparison]::Ordinal) -lt 0) {
            throw "Target profile contract drift; missing: $requiredValue"
        }
    }

    if ($actualLock.target.repository_url -cne $schema.properties.target.properties.repository_url.const) {
        throw 'Target lock does not satisfy the schema repository constant.'
    }
    Write-Output "PASS Phase 0 target contract verified"
}
else {
    foreach ($name in $paths.Keys) {
        Write-Utf8NoBom -Path $paths[$name] -Content ([string]$snapshot[$name])
    }
    Write-Output "PASS Phase 0 target contract generated"
}

Write-Output "commit_sha=$ExpectedCommit"
Write-Output "in_scope_files=$($snapshot.file_count)"
Write-Output "target_file_list_sha256=$($snapshot.target_file_list_sha256)"
Write-Output "scope_sha256=$($snapshot.scope_sha256)"
