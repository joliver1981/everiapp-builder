<#
  Robustly stop AIHub dev processes so a restart ALWAYS serves fresh code.

  Why this exists: start.bat used `taskkill /FI WINDOWTITLE` (no /T) plus a single
  `taskkill /PID <:8800 listener>`. But `uvicorn --reload` runs a SUPERVISOR + a
  WORKER (+ multiprocessing spawn children). Killing the window orphans the
  children; killing the worker just makes the supervisor respawn it. So an orphaned
  tree kept holding :8800 across runs and the NEW server failed to bind
  (Errno 10048) - leaving you on STALE code (a backend was found 39.5h old).

  This stops processes by COMMAND LINE (the uvicorn backend + its spawn children
  and the agent) and frees the ports directly, so nothing survives.

  Usage:  stop-aihub.ps1 [all|backend|frontend|agent]   (default: all)

  NOTE: ASCII only (no smart quotes / em-dashes) so Windows PowerShell 5.1 parses it.
#>
param([string]$Scope = "all")

function Stop-Tree([int[]]$rootPids) {
  if (-not $rootPids) { return }
  # Include direct children (uvicorn --reload spawn workers) before the parents.
  $kids = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
          Where-Object { $rootPids -contains $_.ParentProcessId }
  @($kids.ProcessId) + $rootPids | Select-Object -Unique | ForEach-Object {
    Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
  }
}

function Kill-Cmdline([string]$pattern) {
  # Match python.exe AND uvicorn.exe (a uvicorn-console-script backend shows up as
  # uvicorn.exe, NOT python.exe - missing it left a stale backend holding :8800).
  $p = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
       Where-Object { ($_.Name -eq 'python.exe' -or $_.Name -eq 'uvicorn.exe') -and
                      $_.CommandLine -and $_.CommandLine -match $pattern }
  Stop-Tree @($p.ProcessId)
}

function Kill-CmdHosts([string]$pattern) {
  # Close leftover `cmd /k` host windows for our services. After the child python
  # is killed, the cmd window stays at an idle prompt; its cmdline still shows the
  # launch command, so we match on that and close them (with any live child) so
  # they do not pile up across restarts.
  $c = Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" -ErrorAction SilentlyContinue |
       Where-Object { $_.CommandLine -and $_.CommandLine -match $pattern }
  Stop-Tree @($c.ProcessId)
}

function Free-Port([int]$port) {
  for ($i = 0; $i -lt 6; $i++) {
    $own = (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue).OwningProcess |
           Select-Object -Unique
    if (-not $own) { break }
    # Kill the owner AND its children. A uvicorn --reload worker can keep holding
    # the port while its dead supervisor leaks the inherited socket handle — the
    # listener's OwningProcess then reports the DEAD parent, so a plain
    # Stop-Process is a no-op and the socket survives. Stop-Tree also kills the
    # live spawn-worker child (ParentProcessId = that owner), which frees it.
    Stop-Tree @($own)
    Start-Sleep -Milliseconds 400
  }
}

if ($Scope -eq "all" -or $Scope -eq "backend") {
  Kill-Cmdline 'backend\.src\.main:app'   # uvicorn supervisor(s) + their spawn workers
  Kill-CmdHosts 'AIHub Backend'           # the cmd /k window
  Free-Port 8800                          # safety net: an orphaned worker still listening
}
if ($Scope -eq "all" -or $Scope -eq "agent") {
  Kill-Cmdline 'aihub_agent'
  Kill-CmdHosts 'AIHub Agent'
  Free-Port 8765
}
if ($Scope -eq "all" -or $Scope -eq "frontend") {
  Kill-CmdHosts 'AIHub Frontend'          # the npm run dev window
  Free-Port 5173                          # Vite / node dev server
}

Write-Host "AIHub processes stopped ($Scope)."
