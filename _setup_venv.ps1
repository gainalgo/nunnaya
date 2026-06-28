$pythonDir = "$env:LOCALAPPDATA\Programs\Python\Python313"
$env:Path = "$pythonDir;$pythonDir\Scripts;$env:Path"

Set-Location $PSScriptRoot

# Remove old broken venv if exists
if (Test-Path "venv") {
    Write-Host "[venv] Removing old venv..." -ForegroundColor Yellow
    Remove-Item -Recurse -Force "venv"
}

Write-Host "[venv] Creating new venv with Python 3.13..." -ForegroundColor Cyan
python -m venv venv

Write-Host "[venv] Activating..." -ForegroundColor Cyan
. .\venv\Scripts\Activate.ps1

Write-Host "[pip] Upgrading pip..." -ForegroundColor Cyan
python -m pip install --upgrade pip

Write-Host "[pip] Installing requirements..." -ForegroundColor Cyan
python -m pip install -r requirements.txt

Write-Host ""
Write-Host "=== Setup Complete ===" -ForegroundColor Green
python --version
pip --version
