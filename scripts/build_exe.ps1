# WokBee Windows exe build (PyInstaller)
# Run from repo root: powershell -ExecutionPolicy Bypass -File scripts/build_exe.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$Python = Join-Path $Root ".venv\Scripts\python.exe"
$PyInstaller = Join-Path $Root ".venv\Scripts\pyinstaller.exe"

if (-not (Test-Path $Python)) {
    Write-Error "Missing .venv. Run: python -m venv .venv; .venv\Scripts\activate; pip install -r requirements.txt"
}

& $Python -m pip install -q pyinstaller

Write-Host "Building WokBee..." -ForegroundColor Cyan

& $PyInstaller `
    --noconfirm `
    --clean `
    --name WokBee `
    --windowed `
    --icon "src\tokbee\resources\icon.ico" `
    --paths "src" `
    --collect-all PySide6 `
    --collect-all deepagents `
    --collect-all lark_oapi `
    --collect-all weixin_ilink `
    --collect-all cryptography `
    --collect-all websockets `
    --collect-all pycryptodome `
    --collect-submodules langchain_openai `
    --collect-submodules langgraph `
    --collect-submodules langchain_mcp_adapters `
    --add-data "src\tokbee\resources;tokbee\resources" `
    "main.py"

if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Done: dist\WokBee\WokBee.exe" -ForegroundColor Green
