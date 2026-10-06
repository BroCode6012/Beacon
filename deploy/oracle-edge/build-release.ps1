[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OutPath,
    [string]$Ref = "HEAD"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$output = [System.IO.Path]::GetFullPath($OutPath)
$outputDirectory = Split-Path -Parent $output
if (-not $outputDirectory) {
    $outputDirectory = (Get-Location).Path
}
New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
if (Test-Path -LiteralPath $output) {
    throw "Refusing to overwrite existing release: $output"
}

$commit = (& git -C $repo rev-parse --verify "$Ref`^{commit}").Trim()
if ($LASTEXITCODE -ne 0 -or -not $commit) {
    throw "Could not resolve Git ref '$Ref'. Commit the intended edge state first."
}

$paths = @("requirements-edge.txt", "beacon_engine")
$deployPaths = @(& git -C $repo ls-tree --name-only $commit -- deploy/oracle-edge)
if ($LASTEXITCODE -ne 0) {
    throw "Could not inspect deployment assets in commit $commit"
}
if ($deployPaths.Count -gt 0) {
    $paths += "deploy/oracle-edge"
}

& git -C $repo archive `
    --format=tar.gz `
    --prefix=signal-edge/ `
    "--output=$output" `
    $commit `
    -- `
    @paths
if ($LASTEXITCODE -ne 0) {
    throw "git archive failed"
}

$stream = [System.IO.File]::OpenRead($output)
$sha = [System.Security.Cryptography.SHA256]::Create()
try {
    $hash = [System.BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '').ToLowerInvariant()
} finally {
    $sha.Dispose()
    $stream.Dispose()
}
$hashPath = "$output.sha256"
"$hash  $([System.IO.Path]::GetFileName($output))" |
    Set-Content -LiteralPath $hashPath -Encoding ascii

[pscustomobject]@{
    Commit = $commit
    Archive = $output
    Sha256 = $hash
    ChecksumFile = $hashPath
}
