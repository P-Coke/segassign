param(
    [switch]$SkipArchive,
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BuildRoot = Join-Path $ProjectRoot "build\pyinstaller"
$DistRoot = Join-Path $ProjectRoot "release"
if ($Version) {
    $DistRoot = Join-Path $DistRoot "v$Version"
}
$ReleaseRoot = Join-Path $DistRoot "SegAssign"
$Spec = Join-Path $PSScriptRoot "segassign.spec"

Push-Location $ProjectRoot
try {
    mamba run -n Geo-Workspace python -m PyInstaller `
        --noconfirm `
        --clean `
        --workpath $BuildRoot `
        --distpath $DistRoot `
        $Spec
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

$Executable = Join-Path $ReleaseRoot "SegAssign.exe"
if (-not (Test-Path -LiteralPath $Executable)) {
    throw "Release executable is missing: $Executable"
}
$BusinessExecutables = Get-ChildItem -LiteralPath $ReleaseRoot -File -Filter "*.exe"
if (@($BusinessExecutables).Count -ne 1) {
    throw "Release root must contain exactly one business executable."
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "RELEASE_README.txt") -Destination $ReleaseRoot -Force
Copy-Item -LiteralPath (Join-Path $ProjectRoot "LICENSE") -Destination $ReleaseRoot -Force
Copy-Item -LiteralPath (Join-Path $ProjectRoot "THIRD_PARTY_NOTICES.md") -Destination $ReleaseRoot -Force

$Manifest = Join-Path $ReleaseRoot "SHA256SUMS.txt"
$HashLines = Get-ChildItem -LiteralPath $ReleaseRoot -File -Recurse |
    Where-Object { $_.FullName -ne $Manifest } |
    Sort-Object FullName |
    ForEach-Object {
        $Hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        $Relative = $_.FullName.Substring($ReleaseRoot.Length + 1).Replace("\", "/")
        "$Hash  $Relative"
    }
Set-Content -LiteralPath $Manifest -Value $HashLines -Encoding utf8

if (-not $SkipArchive) {
    $ArchiveName = if ($Version) { "SegAssign-v$Version-Windows-x64.zip" } else { "SegAssign-Windows-x64.zip" }
    $Archive = Join-Path $DistRoot $ArchiveName
    if (Test-Path -LiteralPath $Archive) {
        Remove-Item -LiteralPath $Archive -Force
    }
    Compress-Archive -LiteralPath $ReleaseRoot -DestinationPath $Archive -CompressionLevel Optimal
}

Write-Output "Release ready: $ReleaseRoot"
