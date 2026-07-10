"""App process manager — spawns Vite dev servers for generated apps."""
import asyncio
import logging
import os
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ..config import settings
from ..apps.provisioning import try_copy_template_node_modules
from .. import node_env

logger = logging.getLogger(__name__)


@dataclass
class AppProcess:
    app_id: str
    port: int
    process: subprocess.Popen | None = None
    status: str = "stopped"  # starting, running, stopped, error
    source: str = "draft"    # draft or v{N}
    error: str | None = None
    # Streaming progress so the UI can show "Installing dependencies..." instead
    # of a silent spinner. phase is the coarse stage; phase_detail is a short
    # human-readable string for the current step.
    phase: str = "idle"
    # queued | installing | spawning | waiting | running | failed | stopped
    phase_detail: str = ""
    phase_started_at: float = 0.0  # monotonic seconds — drives elapsed-time display


class RuntimeManager:
    def __init__(self):
        self._processes: dict[str, AppProcess] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._port_pool: set[int] = set(
            range(settings.app_base_port, settings.app_base_port + settings.app_max_instances)
        )
        self._used_ports: set[int] = set()

    def _get_lock(self, app_id: str) -> asyncio.Lock:
        if app_id not in self._locks:
            self._locks[app_id] = asyncio.Lock()
        return self._locks[app_id]

    @staticmethod
    def _port_is_listening(port: int) -> bool:
        """True if something is already LISTENING on the port (e.g. an orphaned
        preview Vite from a prior backend instance)."""
        import socket
        s = socket.socket()
        s.settimeout(0.25)
        try:
            return s.connect_ex(("127.0.0.1", port)) == 0
        except OSError:
            return False
        finally:
            s.close()

    @staticmethod
    def _kill_orphan_on_port(port: int) -> None:
        """Kill any process LISTENING on `port` before we spawn Vite there. The
        dev backend gets restarted a lot, which ORPHANS the Vite servers it
        spawned (they keep holding their ports). Reusing such a port makes the
        new Vite die with --strictPort "port in use" (exit 1) while the health
        poll succeeds against the orphan — surfacing as the dreaded
        "Process exited unexpectedly (code 1)" a second after preview opens."""
        if sys.platform != "win32":
            return  # _allocate_port already steers clear of held ports on posix
        try:
            out = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True, timeout=5,
            ).stdout
            pids: set[str] = set()
            for line in out.splitlines():
                parts = line.split()
                # Proto  LocalAddr  ForeignAddr  State  PID
                if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(f":{port}"):
                    pids.add(parts[4])
            for pid in pids:
                subprocess.run(["taskkill", "/F", "/T", "/PID", pid],
                               capture_output=True, timeout=5)
                logger.warning("killed orphan PID %s holding preview port %d", pid, port)
        except Exception:
            pass

    def _allocate_port(self) -> int:
        available = sorted(self._port_pool - self._used_ports)
        if not available:
            raise RuntimeError("No available ports — max running apps reached")
        # Prefer a port nothing is ACTUALLY listening on — don't collide with an
        # orphaned preview from a prior backend instance (tracking-only state
        # resets on restart, but the orphaned Vite keeps holding the port).
        for port in available:
            if not self._port_is_listening(port):
                self._used_ports.add(port)
                return port
        # All tracking-free ports are OS-held by orphans; take the lowest and
        # _do_start will kill the orphan right before spawning.
        port = available[0]
        self._used_ports.add(port)
        return port

    def _release_port(self, port: int) -> None:
        self._used_ports.discard(port)

    def _resolve_app_dir(self, app_id: str, source: str) -> Path:
        base = Path(settings.app_data_dir).resolve() / app_id
        if source == "draft":
            return base / "draft" / "frontend"
        else:
            # source is "v1", "v2", etc.
            return base / "versions" / source

    async def start_app(self, app_id: str, source: str = "draft") -> AppProcess:
        """Reserve a slot and kick off the actual start work in the background.

        Returns immediately with `status='starting'` so the HTTP request can
        return and the client can poll get_status() to see real-time progress
        through phases (queued -> installing -> spawning -> waiting -> running).

        Previously this blocked the request for ~30-60s on first launch (npm
        install + vite ready poll), making the UI look frozen.
        """
        import time as _time
        lock = self._get_lock(app_id)
        async with lock:
            # If already running with same source, return existing
            existing = self._processes.get(app_id)
            if existing and existing.status == "running" and existing.source == source:
                return existing

            # Stop existing if different source or errored
            if existing and existing.status in ("running", "starting"):
                await self._stop_process(existing)

            port = self._allocate_port()
            app_proc = AppProcess(
                app_id=app_id, port=port, source=source, status="starting",
                phase="queued", phase_detail="queued for startup",
                phase_started_at=_time.monotonic(),
            )
            self._processes[app_id] = app_proc

        # Fire the actual work outside the lock so the HTTP request returns now.
        asyncio.create_task(self._do_start(app_proc, source))
        return app_proc

    def _set_phase(self, app_proc: AppProcess, phase: str, detail: str = "") -> None:
        import time as _time
        app_proc.phase = phase
        app_proc.phase_detail = detail
        app_proc.phase_started_at = _time.monotonic()
        logger.info("runtime %s phase=%s detail=%r", app_proc.app_id, phase, detail)

    async def _do_start(self, app_proc: AppProcess, source: str) -> None:
        """The slow part of starting a runtime: npm install, vite spawn, ready poll.

        Runs as a background task. Updates app_proc.phase as it goes so the
        client's polling of get_status() shows live progress.
        """
        app_id = app_proc.app_id
        port = app_proc.port
        app_dir = self._resolve_app_dir(app_id, source)

        if not app_dir.exists():
            app_proc.status = "error"
            app_proc.phase = "failed"
            app_proc.phase_detail = "app directory missing"
            app_proc.error = f"App directory not found: {app_dir}"
            self._release_port(port)
            return

        try:
            # 1. Provision dependencies if missing. Scaffold no longer bundles
            # node_modules (it cost ~40s on POST /api/apps), so the first start
            # copies the template's node_modules offline when the app's declared
            # deps still match; anything else falls back to a real npm install.
            node_modules = app_dir / "node_modules"
            if not node_modules.exists():
                self._set_phase(app_proc, "installing",
                                "preparing dependencies (one-time, ~30-60s)")
                if not await try_copy_template_node_modules(app_dir):
                    self._set_phase(app_proc, "installing",
                                    f"npm install in {source} (one-time, ~30-60s)")
                    await self._npm_install(app_dir)

            # 2. Spawn Vite — use absolute path to avoid cwd doubling
            self._set_phase(app_proc, "spawning", "starting vite dev server")
            self._kill_orphan_on_port(port)  # clear any orphaned preview holding this port
            vite_bin = (app_dir / "node_modules" / "vite" / "bin" / "vite.js").resolve()
            if not vite_bin.exists():
                vite_bin = (app_dir / "node_modules" / ".bin" / "vite").resolve()

            cmd = [
                node_env.node_cmd(),
                str(vite_bin),
                "--port", str(port),
                "--host", "0.0.0.0",
                "--strictPort",
            ]
            creation_flags = 0
            if sys.platform == "win32":
                creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP

            env = os.environ.copy()
            # Serve the app under the platform proxy's base path. The Preview iframe loads it via
            # /apps/{id}/ (the runtime proxy), which injects the SDK runtime globals
            # (window.__AIHUB_APP_ID__ / __AIHUB_TOKEN__ / __AIHUB_USER__) and makes the app
            # same-origin with /api so useDataset/useAppDB work. Vite reads this:
            # `base: process.env.VITE_BASE || '/'` (app-template/vite.config.ts) and serves its
            # assets + HMR websocket under that base, which proxy_websocket/proxy_http forward.
            env["VITE_BASE"] = f"/apps/{app_id}/"
            # Redirect Vite's output to a per-app log FILE, never a PIPE. An
            # undrained PIPE fills its OS buffer (tiny on Windows) once the
            # browser loads the app, and Vite then dies on its next write →
            # the dreaded "Process exited unexpectedly (code 1)" with the preview
            # crashing seconds after it opened. A file never blocks. The child
            # keeps its own inherited handle, so we close the parent copy right
            # after spawning.
            vite_log = app_dir / ".vite-dev.log"
            _log_fh = open(vite_log, "wb")
            try:
                proc = subprocess.Popen(
                    cmd, cwd=str(app_dir),
                    # DEVNULL stdin: Vite otherwise inherits the backend console's
                    # stdin for its "h + enter" shortcuts, and an orphaned Vite
                    # crashes with `Error: read EPIPE` at a random later moment
                    # when that console dies — leaving a dead preview behind a
                    # toolbar that still says Running.
                    stdin=subprocess.DEVNULL,
                    stdout=_log_fh, stderr=subprocess.STDOUT,
                    creationflags=creation_flags, env=env,
                )
            finally:
                _log_fh.close()  # parent copy; the child keeps writing to the file
            app_proc.process = proc

            # 3. Wait for vite to actually serve the app (~3-15s usually)
            self._set_phase(app_proc, "waiting",
                            f"polling http://127.0.0.1:{port}/apps/{app_id}/")
            ready = await self._wait_for_ready(port, app_id, timeout=30)
            # Require BOTH: the port answers AND our process is still alive. If the
            # poll succeeds but our Vite already exited, we collided with a leftover
            # on the port (the poll hit the orphan) — fall through to the error path
            # which reads .vite-dev.log and reports the real reason.
            if ready and proc.poll() is None:
                app_proc.status = "running"
                self._set_phase(app_proc, "running", f"http://localhost:{port}/")
                logger.info("App %s running on port %d (source=%s)", app_id, port, source)
                return

            # Vite didn't come up — capture why
            if proc.poll() is not None:
                try:
                    stderr = vite_log.read_text(encoding="utf-8", errors="replace")[-1500:]
                except Exception:
                    stderr = ""
                app_proc.error = f"Process exited with code {proc.returncode}: {stderr[:500]}"
            else:
                app_proc.error = "Startup timeout — server did not become ready in 30s"
            app_proc.status = "error"
            self._set_phase(app_proc, "failed", app_proc.error[:120])
            self._release_port(port)

        except Exception as e:
            app_proc.status = "error"
            app_proc.error = str(e)
            self._set_phase(app_proc, "failed", str(e)[:120])
            self._release_port(port)
            logger.exception("Failed to start app %s", app_id)

    async def stop_app(self, app_id: str) -> None:
        lock = self._get_lock(app_id)
        async with lock:
            proc = self._processes.get(app_id)
            if proc:
                await self._stop_process(proc)
                del self._processes[app_id]

    def get_status(self, app_id: str) -> AppProcess | None:
        proc = self._processes.get(app_id)
        if proc and proc.process:
            # Check if process is still alive
            if proc.process.poll() is not None and proc.status == "running":
                proc.status = "error"
                proc.error = f"Process exited unexpectedly (code {proc.process.returncode})"
                self._release_port(proc.port)
        return proc

    async def shutdown_all(self) -> None:
        logger.info("Shutting down all app processes (%d running)...", len(self._processes))
        for app_id, proc in list(self._processes.items()):
            try:
                await self._stop_process(proc)
            except Exception:
                logger.exception("Error stopping app %s", app_id)
        self._processes.clear()
        self._used_ports.clear()

    async def _stop_process(self, proc: AppProcess) -> None:
        if proc.process and proc.process.poll() is None:
            try:
                if sys.platform == "win32":
                    proc.process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    proc.process.terminate()

                # Wait up to 5 seconds for graceful shutdown
                for _ in range(50):
                    if proc.process.poll() is not None:
                        break
                    await asyncio.sleep(0.1)

                # Force kill if still running
                if proc.process.poll() is None:
                    proc.process.kill()
                    proc.process.wait()
            except Exception:
                logger.exception("Error killing process for app %s", proc.app_id)

        self._release_port(proc.port)
        proc.status = "stopped"
        proc.process = None

    async def _npm_install(self, app_dir: Path) -> None:
        """Run npm install in the app directory.

        Uses subprocess.run in a thread because asyncio subprocess support
        is not available on Windows with the SelectorEventLoop (uvicorn default).
        """
        npm_cmd = node_env.npm_cmd()

        def _run():
            result = subprocess.run(
                [npm_cmd, "install", "--no-audit", "--no-fund"],
                cwd=str(app_dir),
                capture_output=True,
                timeout=120,
            )
            if result.returncode != 0:
                stderr = result.stderr.decode(errors="replace")[:500]
                raise RuntimeError(
                    f"npm install failed (code {result.returncode}): {stderr}"
                )

        loop = asyncio.get_event_loop()
        await asyncio.wait_for(loop.run_in_executor(None, _run), timeout=130)

    async def _wait_for_ready(self, port: int, app_id: str, timeout: int = 30) -> bool:
        """Poll the Vite dev server until the ACTUAL app document serves.

        Vite runs with base=/apps/{app_id}/, so `/` is just its 404 hint page —
        polling that (as we used to, accepting any <500) declared "running" the
        moment the port answered, without ever exercising the HTML path the
        Preview iframe is about to load. Require a 200 from the real base path
        so "running" means "the iframe's first request will succeed".
        """
        url = f"http://127.0.0.1:{port}/apps/{app_id}/"
        deadline = asyncio.get_event_loop().time() + timeout
        async with httpx.AsyncClient() as client:
            while asyncio.get_event_loop().time() < deadline:
                try:
                    # 3s per attempt: the first index.html transform on a cold
                    # machine can exceed 1s; give it room instead of aborting.
                    resp = await client.get(url, timeout=3.0)
                    if resp.status_code == 200:
                        return True
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.25)
        return False


# Singleton
runtime_manager = RuntimeManager()
