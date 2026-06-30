# AIHub Development Server Startup Script
# Starts both backend (FastAPI) and frontend (Vite) dev servers

Write-Host "Starting AIHub Development Servers..." -ForegroundColor Cyan
Write-Host ""

# Check for .env file
$envFile = Join-Path $PSScriptRoot ".." ".env"
if (-not (Test-Path $envFile)) {
    Write-Host "No .env file found. Copying from .env.example..." -ForegroundColor Yellow
    Copy-Item (Join-Path $PSScriptRoot ".." ".env.example") $envFile
    Write-Host "Please edit .env with your configuration before using AI features." -ForegroundColor Yellow
    Write-Host ""
}

# Start backend
Write-Host "[Backend] Starting FastAPI on http://localhost:8800" -ForegroundColor Green
$backendDir = Join-Path $PSScriptRoot ".." "backend"
$venvPython = Join-Path $PSScriptRoot ".." ".venv" "Scripts" "python.exe"

Start-Process -FilePath $venvPython -ArgumentList "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8800", "--reload" -WorkingDirectory (Join-Path $PSScriptRoot "..") -NoNewWindow

# Start frontend
Write-Host "[Frontend] Starting Vite on http://localhost:5173" -ForegroundColor Green
$frontendDir = Join-Path $PSScriptRoot ".." "frontend"

Start-Process -FilePath "npm" -ArgumentList "run", "dev" -WorkingDirectory $frontendDir -NoNewWindow

Write-Host ""
Write-Host "AIHub is running!" -ForegroundColor Cyan
Write-Host "  Frontend: http://localhost:5173" -ForegroundColor White
Write-Host "  Backend:  http://localhost:8800" -ForegroundColor White
Write-Host "  API Docs: http://localhost:8800/docs" -ForegroundColor White
Write-Host ""
Write-Host "Mock AD credentials:" -ForegroundColor Yellow
Write-Host "  admin/password    - Admin role" -ForegroundColor White
Write-Host "  developer/password - Developer role" -ForegroundColor White
Write-Host "  user/password     - User role" -ForegroundColor White
Write-Host ""
Write-Host "Press Ctrl+C to stop all servers." -ForegroundColor Gray

# Wait for Ctrl+C
try {
    while ($true) { Start-Sleep -Seconds 1 }
} finally {
    Write-Host "`nStopping servers..." -ForegroundColor Yellow
}
