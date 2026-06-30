@echo off
setlocal EnableDelayedExpansion
title AIHub Platform
cd /d "%~dp0"

echo.
echo  ========================================
echo   AIHub - AI App Platform
echo  ========================================
echo.

rem --- 1. Stop previous instances ---------------------------------
echo  Stopping any previous AIHub instances...
rem  Robustly stop prior backend/frontend/agent by COMMAND LINE + port. The old
rem  window-title taskkills lacked /T, so uvicorn --reload child workers were
rem  ORPHANED and kept holding :8800 across runs — leaving the server on STALE code.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop-aihub.ps1" all >nul 2>&1

rem  Fallback (and belt-and-suspenders): free the ports the blunt way, with /T so
rem  the whole process tree dies, not just the window.
taskkill /F /T /FI "WINDOWTITLE eq AIHub Backend*"  >nul 2>&1
taskkill /F /T /FI "WINDOWTITLE eq AIHub Frontend*" >nul 2>&1
taskkill /F /T /FI "WINDOWTITLE eq AIHub Agent*"    >nul 2>&1
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":8800 .*LISTENING"') do taskkill /F /T /PID %%P >nul 2>&1
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":5173 .*LISTENING"') do taskkill /F /T /PID %%P >nul 2>&1
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":8765 .*LISTENING"') do taskkill /F /T /PID %%P >nul 2>&1

rem --- 2. Rebuild only when manifests are newer than installs -----
set "VENV_MARKER=.venv\Scripts\python.exe"
call :needs_rebuild "backend\pyproject.toml" "%VENV_MARKER%"
if !ERRORLEVEL! EQU 0 (
    echo  Installing backend dependencies...
    if not exist .venv python -m venv .venv
    call .venv\Scripts\activate.bat && pip install -e backend[dev]
    call .venv\Scripts\deactivate.bat >nul 2>&1
)

rem  Agent gets installed into the same venv so we don't need a second one.
set "AGENT_MARKER=.venv\Lib\site-packages\aihub_agent\__init__.py"
call :needs_rebuild "aihub-agent\pyproject.toml" "%AGENT_MARKER%"
if !ERRORLEVEL! EQU 0 (
    echo  Installing aihub-agent...
    call .venv\Scripts\activate.bat && pip install -e aihub-agent
    call .venv\Scripts\deactivate.bat >nul 2>&1
)

rem  Playwright browser (chromium) — needed by the AI self-verify runtime
rem  probe to catch React mount errors / blank-page bugs. ~150MB one-time.
if not exist "%USERPROFILE%\AppData\Local\ms-playwright\chromium_headless_shell-*" (
    echo  Installing Playwright Chromium ^(one-time, ^~150MB^)...
    call .venv\Scripts\activate.bat && python -m playwright install chromium
    call .venv\Scripts\deactivate.bat >nul 2>&1
)

call :needs_rebuild "frontend\package.json" "frontend\node_modules\.package-lock.json"
if !ERRORLEVEL! EQU 0 (
    echo  Installing frontend dependencies...
    pushd frontend && call npm install && popd
)

rem --- 3. Start servers -------------------------------------------
echo  Starting backend (port 8800)...
rem  NO --reload: it runs a supervisor + worker, and a killed/edited worker
rem  orphans the :8800 socket (the dead supervisor leaks the inherited handle),
rem  so the next start hits Errno 10048 and serves nothing. Single-process binds
rem  reliably and is killed cleanly by command line. Restart to pick up changes.
start "AIHub Backend" cmd /k "cd /d "%~dp0" && .venv\Scripts\activate && uvicorn backend.src.main:app --host 0.0.0.0 --port 8800"

echo  Starting frontend (port 5173)...
start "AIHub Frontend" cmd /k "cd /d "%~dp0\frontend" && npm run dev"

echo  Starting aihub-agent (port 8765)...
rem  Dev-only AGENT_TOKEN. Use this same value when storing it as an
rem  `agent_token` Secret in Admin > Secrets, then reference it when
rem  registering a Deployment Target in Admin > Deployment Targets.
rem  NOTE: the `set "VAR=value"` quoted form is REQUIRED here. Without the
rem  quotes, Windows batch includes the trailing space before `&&` in the
rem  variable value, e.g. AGENT_HOST becomes "0.0.0.0 " and uvicorn fails to
rem  resolve it with [Errno 11001] getaddrinfo failed.
start "AIHub Agent" cmd /k "cd /d "%~dp0" && .venv\Scripts\activate && set "AGENT_TOKEN=aihub-dev-token" && set "AGENT_HOST=0.0.0.0" && set "AGENT_PORT=8765" && python -m aihub_agent"

rem  Give the backend + frontend a moment to bind before opening the browser.
timeout /t 5 /nobreak >nul

rem  Open the AIHub UI in the default browser.
start "" "http://localhost:5173"

echo.
echo  Ready!
echo    Frontend:    http://localhost:5173
echo    Backend API: http://localhost:8800/docs
echo    Agent API:   http://localhost:8765/api/v1/info  (Bearer aihub-dev-token)
echo.
echo  Credentials:
echo    admin / password      (Admin)
echo    developer / password  (Developer)
echo    user / password       (User)
echo.
echo  To test deployment to the local agent (first run only):
echo    1. Sign in as admin
echo    2. Admin -^> Secrets: Add Secret
echo         name     = local-agent-token
echo         category = agent_token
echo         value    = aihub-dev-token
echo    3. Admin -^> Deployment Targets: Add Target
echo         kind        = Agent
echo         host        = localhost
echo         agent port  = 8765
echo         port range  = 9100-9120
echo         credential  = local-agent-token
echo       Click Test - expect green check
echo    4. Open an app in the Builder, Publish v1, then click the Rocket
echo       icon in the top bar and Deploy v1 to local-agent
echo    5. Open the public URL it returns - app loads from a real prod build
echo.
echo  To test bug reports + AI auto-fix (after step 4):
echo    1. In the Builder top bar, click Bugs to enable the widget
echo    2. Optionally set Auto-fix to "low risk"
echo    3. Open the deployed app, click the red bug button, file a report
echo    4. Watch the report appear in Admin -^> Bug Reports
echo.
echo  Close this window or press any key to exit.
echo  (Backend, frontend, and agent stay running in their own windows.)
pause >nul
exit /b 0

:needs_rebuild
rem %1 = manifest path, %2 = marker path. Exit 0 (true) if rebuild needed.
if not exist %2 exit /b 0
for %%a in (%1) do set "M1=%%~ta"
for %%a in (%2) do set "M2=%%~ta"
if "!M1!" GTR "!M2!" exit /b 0
exit /b 1
