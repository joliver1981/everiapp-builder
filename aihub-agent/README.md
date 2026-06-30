# aihub-agent

Deployment agent for the AIHub Platform. Runs on each target host. AIHub pushes built app artifacts (a tarball of `dist/`) to it over HTTP; the agent unpacks them, picks a free port from its configured range, and serves each app as a managed subprocess.

There are **three ways to run** the agent. Pick the one that matches who's installing it.

| Audience | Method | What's needed on the target |
|---|---|---|
| Non-technical operators (production deployment to Windows servers) | [Windows installer](#windows-installer) | Nothing — installer bundles Python |
| Dev / internal testing on Windows | `start.bat` from the project root | The repo + Python (already there in dev) |
| Linux / macOS / other | [From source](#run-from-source) | Python 3.10+ |

---

## Windows installer

The recommended way for real deployments to remote Windows machines. The installer:

- Drops a single `aihub-agent.exe` (no Python required on the target)
- Registers a Windows service `AIHubAgent` that auto-starts on boot
- Prompts you for the agent token during setup (paste from AIHub's Admin → Secrets)
- Opens TCP 8765 in the Windows firewall
- Auto-restarts on crash via [NSSM](https://nssm.cc)
- Logs to `%ProgramData%\aihub-agent\logs\`

### Installing (for operators)

1. Download `aihub-agent-setup-X.Y.Z.exe` from the AIHub release page
2. Right-click → Run as administrator → click through "Next"
3. When prompted, paste the agent token from AIHub (Admin → Secrets → category=`agent_token`)
4. Finish. The agent is now running as a Windows service.

To confirm:
```cmd
sc query AIHubAgent
curl -H "Authorization: Bearer <your-token>" http://localhost:8765/api/v1/info
```

To stop/start/restart:
```cmd
sc stop AIHubAgent
sc start AIHubAgent
```

Logs:
- `%ProgramData%\aihub-agent\logs\service.out.log`
- `%ProgramData%\aihub-agent\logs\service.err.log`

### Uninstalling

Control Panel → Programs → AIHub Agent → Uninstall. Removes the service, firewall rule, and install dir. Leaves `%ProgramData%\aihub-agent\` (deployed apps + logs) so you can recover; delete manually if you want a clean slate.

### Building the installer (for the repo maintainer)

One-time setup:

1. **Install Inno Setup 6** from https://jrsoftware.org/isdl.php
2. **Download NSSM**:
   - Get `nssm-2.24.zip` (or newer) from https://nssm.cc/download
   - Extract `win64/nssm.exe`
   - Place it at `aihub-agent/installer/vendor/nssm.exe`

Build (one click):

```cmd
cd C:\src\aihub-apps\aihub-agent
installer\Build_AIHub_Agent.bat
```

That single command:
1. Verifies the project venv + PyInstaller + vendored nssm.exe + ISCC are present
2. Runs PyInstaller on `installer\aihub-agent.spec` → **OneDir** output at `dist\aihub-agent\` (folder of files, ~50 MB, plus a ~10 MB `aihub-agent.exe` launcher)
3. Smoke-tests the exe to confirm it boots
4. Runs ISCC on `installer\aihub-agent.iss` → `installer\Output\aihub-agent-setup-X.Y.Z.exe` (~20 MB)

Pass `clean` as the first arg to wipe `build\`, `dist\`, `installer\Output\` first:
```cmd
installer\Build_AIHub_Agent.bat clean
```

**Why OneDir, not OneFile?** Folder builds trip Windows Defender / SmartScreen false positives far less often than single-file `--onefile` builds, start faster (no per-launch extraction to `%TEMP%`), and let NSSM point at a stable folder path that won't be re-extracted on every service restart.

The resulting `aihub-agent-setup-X.Y.Z.exe` is what you hand to operators.

---

## Run from source

Useful for development, or on Linux / macOS where the Windows installer doesn't apply.

```bash
pip install -e .
AGENT_TOKEN=change-me python -m aihub_agent
```

By default it binds `:8765` and serves apps on ports `9100-9199`. Configure via env vars or `.env`.

For Linux service registration, write a systemd unit pointing at the `aihub-agent` entry point and set the env vars under `[Service] Environment=`.

---

## Configuration

| Var | Default | Notes |
|---|---|---|
| `AGENT_TOKEN` | _required_ | Bearer token AIHub must send. Store as a Secret on the AIHub side (category `agent_token`). |
| `AGENT_HOST` | `0.0.0.0` | Bind address for the agent's own HTTP server. |
| `AGENT_PORT` | `8765` | Port for the agent's own HTTP server. |
| `AGENT_DATA_DIR` | `%ProgramData%\aihub-agent` (Win) / `/var/lib/aihub-agent` (Unix) | Where unpacked apps + logs live. |
| `APP_PORT_RANGE_START` | `9100` | First port in the app pool. |
| `APP_PORT_RANGE_END` | `9199` | Last port (inclusive). |
| `APP_STARTUP_TIMEOUT` | `20` | Seconds to wait for an app's static server to respond. |
| `PUBLIC_HOST_OVERRIDE` | _empty_ | If set, use this hostname when reporting `public_url` back to AIHub (otherwise use the request `Host` header). |

---

## HTTP API

All routes require `Authorization: Bearer ${AGENT_TOKEN}`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/info` | Agent version, OS, port range, used ports. |
| POST | `/api/v1/apps/{app_id}/deploy` | Multipart: `meta` (JSON `{version, port}`) + `tarball` (`dist.tar.gz`). Unpacks, starts static server, returns `{public_url}`. |
| POST | `/api/v1/apps/{app_id}/stop` | Stops the app, frees the port. |
| GET | `/api/v1/apps/{app_id}/health` | `{running, port, last_probe_at}`. |
| GET | `/api/v1/apps/{app_id}/logs?n=200` | Tail the per-app log file. |
| GET | `/api/v1/apps` | List all known deployed apps on this agent. |

---

## Subcommands (in the frozen exe)

When built as a single Windows exe, `aihub-agent.exe` understands:

```
aihub-agent.exe                            # run the main agent server (default)
aihub-agent.exe serve                      # same as above (explicit)
aihub-agent.exe static-serve --dir X --port Y
                                           # run as a per-app static server
                                           # — the agent spawns one of these for each deployed app
```

The `static-serve` subcommand is what `apps.py` invokes to host each deployed app's `dist/`. It's not for human use directly.

---

## Troubleshooting

**Service won't start after install.** Check `%ProgramData%\aihub-agent\logs\service.err.log`. Most common cause: AGENT_TOKEN env var didn't get set. Fix:
```cmd
nssm edit AIHubAgent
```
(if you have nssm on PATH; otherwise re-run the installer)

**Test from another machine returns ConnectError.** Check firewall — `netsh advfirewall firewall show rule name="AIHub Agent (TCP 8765)"`. The installer adds this rule but enterprise group policy may override.

**AIHub Test returns "Agent has no AGENT_TOKEN configured".** The exe started but no token was passed in env. Re-run the installer or:
```cmd
sc stop AIHubAgent
nssm set AIHubAgent AppEnvironmentExtra "AGENT_TOKEN=your-token" "AGENT_PORT=8765"
sc start AIHubAgent
```

**AV / SmartScreen warning when running the installer.** The installer isn't code-signed (yet). Click "More info" → "Run anyway". For production deployments to client machines, sign the exe with a code-signing certificate before distribution.
