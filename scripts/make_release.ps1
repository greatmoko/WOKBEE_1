# Create forum release zip: WokBee-vX.Y.Z.src.zip
# Run from repo root: powershell -ExecutionPolicy Bypass -File scripts/make_release.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$Version = "0.2.1"
$PyProject = Join-Path $Root "pyproject.toml"
if (Test-Path $PyProject) {
    $m = Select-String -Path $PyProject -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($m) { $Version = $m.Matches.Groups[1].Value }
}

$ReleaseDir = Join-Path $Root "release"
$SrcZip = Join-Path $ReleaseDir "WokBee-v$Version.src.zip"

Write-Host "WokBee src release v$Version" -ForegroundColor Cyan

$ExcludeNames = @(
    ".venv", "venv", "dist", "build", "release",
    ".git", ".cursor", ".idea", ".vscode",
    ".wokbee", "__pycache__", "artifacts",
    ".learnings", ".agents",
    "WokBee.spec"
)

if (Test-Path $ReleaseDir) { Remove-Item $ReleaseDir -Recurse -Force }
New-Item -ItemType Directory -Path $ReleaseDir | Out-Null

$TempSrc = Join-Path $env:TEMP "WokBee-src-$Version"
if (Test-Path $TempSrc) { Remove-Item $TempSrc -Recurse -Force }
New-Item -ItemType Directory -Path $TempSrc | Out-Null

Get-ChildItem -Path $Root -Force | ForEach-Object {
    if ($ExcludeNames -contains $_.Name) { return }
    Copy-Item -Path $_.FullName -Destination (Join-Path $TempSrc $_.Name) -Recurse -Force
}

Write-Host "Creating $SrcZip ..." -ForegroundColor Cyan
Compress-Archive -Path (Join-Path $TempSrc "*") -DestinationPath $SrcZip -Force
Remove-Item $TempSrc -Recurse -Force

Write-Host ""
Write-Host "Release package ready:" -ForegroundColor Green
Write-Host "  $SrcZip"
Write-Host ""
Write-Host "Upload to GitHub Releases or share the zip as needed." -ForegroundColor Yellow
