# AIHub Setup Script for Windows
# Run this once to set up the development environment

Write-Host "AIHub Platform Setup" -ForegroundColor Cyan
Write-Host "====================" -ForegroundColor Cyan
Write-Host ""

$rootDir = Join-Path $PSScriptRoot ".."

# Step 1: Check Python
Write-Host "[1/5] Checking Python..." -ForegroundColor Green
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    $python = Get-Command python3 -ErrorAction SilentlyContinue
}
if (-not $python) {
    Write-Host "ERROR: Python 3.12+ is required. Please install Python first." -ForegroundColor Red
    exit 1
}
& $python.Source --version
Write-Host ""

# Step 2: Check Node.js
Write-Host "[2/5] Checking Node.js..." -ForegroundColor Green
$node = Get-Command node -ErrorAction SilentlyContinue
if (-not $node) {
    Write-Host "ERROR: Node.js 20+ is required. Please install Node.js first." -ForegroundColor Red
    exit 1
}
node --version
Write-Host ""

# Step 3: Create Python venv and install deps
Write-Host "[3/5] Setting up Python virtual environment..." -ForegroundColor Green
$venvDir = Join-Path $rootDir ".venv"
if (-not (Test-Path $venvDir)) {
    & $python.Source -m venv $venvDir
}
$pip = Join-Path $venvDir "Scripts" "pip.exe"
& $pip install -e "$rootDir/backend[dev]"
Write-Host ""

# Step 4: Install frontend deps
Write-Host "[4/5] Installing frontend dependencies..." -ForegroundColor Green
Push-Location (Join-Path $rootDir "frontend")
npm install
Pop-Location
Write-Host ""

# Step 5: Create .env
Write-Host "[5/5] Setting up environment..." -ForegroundColor Green
$envFile = Join-Path $rootDir ".env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $rootDir ".env.example") $envFile

    # Generate a Fernet key
    $venvPython = Join-Path $venvDir "Scripts" "python.exe"
    $fernetKey = & $venvPython -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    (Get-Content $envFile) -replace 'change-me-generate-a-fernet-key', $fernetKey | Set-Content $envFile

    Write-Host "Created .env with generated encryption key" -ForegroundColor Green
} else {
    Write-Host ".env already exists, skipping" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Setup complete!" -ForegroundColor Cyan
Write-Host ""
Write-Host "To start the development servers, run:" -ForegroundColor White
Write-Host "  .\scripts\dev.ps1" -ForegroundColor Yellow
Write-Host ""
Write-Host "Mock AD credentials:" -ForegroundColor White
Write-Host "  admin/password    - Admin role" -ForegroundColor White
Write-Host "  developer/password - Developer role" -ForegroundColor White
Write-Host "  user/password     - User role" -ForegroundColor White
