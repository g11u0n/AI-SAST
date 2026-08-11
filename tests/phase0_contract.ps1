[CmdletBinding()]
param(
    [string]$RepoPath = '',
    [string]$GitExecutable = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ProjectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
if ([string]::IsNullOrWhiteSpace($RepoPath)) {
    $RepoPath = Join-Path $ProjectRoot '.target-src\userland'
}
$Verifier = Join-Path $ProjectRoot 'scripts\verify_target_lock.ps1'
$ArtifactPaths = @(
    (Join-Path $ProjectRoot 'target.lock.yaml'),
    (Join-Path $ProjectRoot 'artifacts\coverage\target_inventory.json'),
    (Join-Path $ProjectRoot 'artifacts\coverage\source_manifest.jsonl'),
    (Join-Path $ProjectRoot 'artifacts\coverage\exclusion_manifest.jsonl'),
    (Join-Path $ProjectRoot 'artifacts\coverage\target_verification.json'),
    (Join-Path $ProjectRoot 'schemas\target-lock.schema.json'),
    (Join-Path $ProjectRoot 'docs\target_profile.md')
)

function Get-ArtifactHashes {
    $result = [ordered]@{}
    foreach ($path in $ArtifactPaths) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Missing test artifact: $path"
        }
        $result[$path] = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
    }
    return $result
}

$before = Get-ArtifactHashes
& $Verifier -RepoPath $RepoPath -GitExecutable $GitExecutable
& $Verifier -RepoPath $RepoPath -GitExecutable $GitExecutable
$after = Get-ArtifactHashes

foreach ($path in $ArtifactPaths) {
    if ($before[$path] -cne $after[$path]) {
        throw "Read-only verification mutated an artifact: $path"
    }
}

$lock = Get-Content -LiteralPath (Join-Path $ProjectRoot 'target.lock.yaml') -Raw | ConvertFrom-Json
if ($lock.expected_inventory.tracked_files -ne 830) { throw 'Expected 830 tracked files.' }
if ($lock.expected_inventory.in_scope_files -ne 654) { throw 'Expected 654 in-scope files.' }
if ($lock.expected_inventory.out_of_scope_files -ne 176) { throw 'Expected 176 out-of-scope files.' }
if ($lock.analysis_scope.exclude_paths.Count -ne 0) { throw 'Tracked C/C++ path exclusions must remain empty.' }
if (($lock.expected_inventory.in_scope_files + $lock.expected_inventory.out_of_scope_files) -ne $lock.expected_inventory.tracked_files) {
    throw 'The target inventory is not a complete partition.'
}

Write-Output 'PASS Phase 0 contract smoke tests'
