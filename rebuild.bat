@echo off
rem  Stop -> rebuild -> start the AIHub dev stack reliably (no stale processes).
rem    rebuild.bat           normal stop + rebuild + start
rem    rebuild.bat -Clean    wipe node_modules + caches first, then rebuild
rem    rebuild.bat -NoStart  stop + rebuild only
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\rebuild-aihub.ps1" %*
echo.
pause
