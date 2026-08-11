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

& (Join-Path $PSScriptRoot 'lock_target.ps1') `
    -RepoPath $RepoPath `
    -GitExecutable $GitExecutable `
    -VerifyOnly

if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
