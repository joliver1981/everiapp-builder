#Requires -Version 5
<#
  rebuild-aihub.ps1 - STOP, REBUILD, and START the AIHub dev stack reliably.

  This is the ONE script that does it all. (start.bat just starts; this one also
  cleans out stale processes and reinstalls deps first.)

  STOP    Kills the backend (uvicorn + its --reload spawn children), the agent,
          and the frontend by COMMAND LINE + port (via stop-aihub.ps1). Nothing
          stale can survive - that is what made restarts serve old code before.
  REBUILD pip install backend[dev] + aihub-agent into .venv, and npm install the
          frontend. -Clean also wipes node_modules + build/pycache caches first.
  START   Launches backend (:8800), frontend (:5173), agent (:8765) in titled
          windows, then VERIFIES the backend actually bound and is serving FRESH
          code (low uptime) before reporting success.

  Usage (from anywhere):
    powershell -ExecutionPolicy Bypass -File scripts\rebuild-aihub.ps1
    powershell -ExecutionPolicy Bypass -File scripts\rebuild-aihub.ps1 -Clean
    powershell -ExecutionPolicy Bypass -File scripts\rebuild-aihub.ps1 -NoStart
  ...or just double-click rebuild.bat in the repo root.

  NOTE: ASCII only (no smart quotes / em-dashes) so Windows PowerShell 5.1 parses it.
#>
[CmdletBinding()]
param(
  [switch]$Clean,      # wipe node_modules + caches, force a clean rebuild
  [switch]$NoStart,    # stop + rebuild only (do not launch the stack)
  [switch]$NoBrowser   # do not open the browser when done
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Root ".venv"
$Py   = Join-Path $Venv "Scripts\python.exe"
Set-Location $Root

function Step($m) { Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Fail($m) { Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }

# ---------- 1. STOP ----------
Step "Stopping any running AIHub processes"
& (Join-Path $PSScriptRoot "stop-aihub.ps1") all
Start-Sleep -Seconds 1

# ---------- 2. CLEAN (optional) ----------
if ($Clean) {
  Step "Cleaning caches + node_modules"
  "frontend\node_modules", "frontend\dist", "backend\.pytest_cache" | ForEach-Object {
    $p = Join-Path $Root $_; if (Test-Path $p) { Remove-Item -Recurse -Force $p -ErrorAction SilentlyContinue }
  }
  Get-ChildItem (Join-Path $Root "backend"), (Join-Path $Root "aihub-agent") -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}

# ---------- 3. REBUILD ----------
if (-not (Test-Path $Py)) { Step "Creating venv"; & python -m venv $Venv; if ($LASTEXITCODE) { Fail "venv create failed" } }

Step "Installing backend + agent (pip)"
& $Py -m pip install -e "backend[dev]" --disable-pip-version-check -q; if ($LASTEXITCODE) { Fail "pip install backend failed" }
& $Py -m pip install -e "aihub-agent" --disable-pip-version-check -q; if ($LASTEXITCODE) { Fail "pip install aihub-agent failed" }

Step "Installing frontend (npm)"
Push-Location (Join-Path $Root "frontend")
& npm install --no-fund --no-audit
$npmRc = $LASTEXITCODE
Pop-Location
if ($npmRc) { Fail "npm install failed" }

if ($NoStart) { Step "Stop + rebuild complete (-NoStart)"; exit 0 }

# ---------- 4. START ----------
Step "Starting backend (:8800), frontend (:5173), agent (:8765)"
# Backend + agent: launch the venv python DIRECTLY (no `cmd /k + activate.bat`,
# which was unreliable when not launched interactively) so they bind dependably.
# Each opens its own console window with live logs; stopping kills them by command
# line, so no orphan windows.
# NO --reload: its supervisor+worker model orphans the :8800 socket when a worker
# is killed/reloaded (dead supervisor leaks the inherited handle), so the next
# start hits Errno 10048 and serves nothing. Single-process binds reliably and is
# killed cleanly by command line.
Start-Process $Py -ArgumentList @("-m", "uvicorn", "backend.src.main:app", "--host", "0.0.0.0", "--port", "8800") -WorkingDirectory $Root | Out-Null
$env:AGENT_TOKEN = "aihub-dev-token"; $env:AGENT_HOST = "0.0.0.0"; $env:AGENT_PORT = "8765"
Start-Process $Py -ArgumentList @("-m", "aihub_agent") -WorkingDirectory $Root | Out-Null
# Frontend (Vite) needs a shell for npm:
Start-Process cmd.exe -ArgumentList '/k', "title AIHub Frontend && npm run dev" -WorkingDirectory (Join-Path $Root "frontend") | Out-Null

# ---------- 5. VERIFY (the reliability check) ----------
Step "Verifying the backend came up FRESH"
$ok = $false
for ($i = 0; $i -lt 90; $i++) {
  Start-Sleep -Seconds 1
  try {
    $h = Invoke-RestMethod "http://localhost:8800/api/health" -TimeoutSec 2
    if ($h.status -eq "healthy") {
      if ($h.uptime_seconds -le 180) {
        Write-Host ("  OK - backend healthy, uptime {0}s (fresh), key={1}" -f $h.uptime_seconds, $h.encryption_key_source) -ForegroundColor Green
      } else {
        Write-Host ("  WARNING - healthy but uptime {0}s: a STALE backend may still hold :8800. Re-run, or check the 'AIHub Backend' window for a bind error." -f $h.uptime_seconds) -ForegroundColor Yellow
      }
      $ok = $true; break
    }
  } catch { }
}
if (-not $ok) {
  Write-Host "  Backend did NOT answer on :8800 within 90s - open the 'AIHub Backend' window to see the bind error or traceback." -ForegroundColor Red
}

$fe = $false
for ($i = 0; $i -lt 20; $i++) {
  Start-Sleep -Seconds 1
  try { Invoke-WebRequest "http://localhost:5173" -UseBasicParsing -TimeoutSec 2 | Out-Null; $fe = $true; break } catch { }
}
if ($fe) { Write-Host "  Frontend up on :5173" } else { Write-Host "  Frontend not responding yet on :5173 (Vite can take a few more seconds)" }

if ($ok -and -not $NoBrowser) { Start-Process "http://localhost:5173" }
Step "Done - stop + rebuild + start complete"
