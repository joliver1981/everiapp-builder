import asyncio
import logging
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .config import settings
from .logs import AppLogStore
from .ports import port_pool

logger = logging.getLogger(__name__)


@dataclass
class DeployedApp:
    app_id: str
    version: int
    port: int
    serve_dir: Path
    status: str = "starting"  # starting, running, stopped, error
    error: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_probe_at: datetime | None = None
    last_probe_ok: bool = False
    process: subprocess.Popen | None = None
    log: AppLogStore | None = None


class AppRegistry:
    def __init__(self):
        self._apps: dict[str, DeployedApp] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, app_id: str) -> asyncio.Lock:
        if app_id not in self._locks:
            self._locks[app_id] = asyncio.Lock()
        return self._locks[app_id]

    def get(self, app_id: str) -> DeployedApp | None:
        return self._apps.get(app_id)

    def list(self) -> list[DeployedApp]:
        return list(self._apps.values())

    async def deploy(self, app_id: str, version: int, port: int, serve_dir: Path) -> DeployedApp:
        async with self._lock(app_id):
            existing = self._apps.get(app_id)
            if existing:
                await self._stop_locked(existing)

            log = AppLogStore(settings.logs_dir, app_id)
            log.clear()
            app = DeployedApp(
                app_id=app_id,
                version=version,
                port=port,
                serve_dir=serve_dir,
                status="starting",
                log=log,
            )
            self._apps[app_id] = app

            try:
                # When PyInstaller-frozen, sys.executable IS the agent.exe and
                # `-m aihub_agent.static_serve` won't work (no source on disk).
                # Use our own static-serve subcommand instead, dispatched by
                # __main__.py. When running from source (dev mode), keep the
                # explicit module invocation so behavior matches pip-installed
                # setups.
                if getattr(sys, "frozen", False):
                    cmd = [
                        sys.executable, "static-serve",
                        "--dir", str(serve_dir),
                        "--port", str(port),
                    ]
                else:
                    cmd = [
                        sys.executable, "-m", "aihub_agent.static_serve",
                        "--dir", str(serve_dir),
                        "--port", str(port),
                    ]
                creation_flags = 0
                if sys.platform == "win32":
                    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP

                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    creationflags=creation_flags,
                    text=True,
                    bufsize=1,
                )
                app.process = proc
                _spawn_log_pump(proc, log)

                ready = await _wait_for_ready(port, timeout=settings.app_startup_timeout)
                if ready:
                    app.status = "running"
                    app.last_probe_ok = True
                    app.last_probe_at = datetime.now(timezone.utc)
                    logger.info("App %s v%d running on port %d", app_id, version, port)
                else:
                    if proc.poll() is not None:
                        app.error = f"Static server exited with code {proc.returncode}"
                    else:
                        app.error = f"Static server did not become ready in {settings.app_startup_timeout}s"
                    app.status = "error"
                    await self._stop_locked(app)
            except Exception as e:
                app.status = "error"
                app.error = str(e)
                logger.exception("Failed to deploy app %s", app_id)
                await self._stop_locked(app)

            return app

    async def stop(self, app_id: str) -> bool:
        async with self._lock(app_id):
            app = self._apps.get(app_id)
            if not app:
                return False
            await self._stop_locked(app)
            del self._apps[app_id]
            return True

    async def stop_all(self) -> None:
        for app_id in list(self._apps.keys()):
            try:
                await self.stop(app_id)
            except Exception:
                logger.exception("Error stopping %s", app_id)

    async def probe(self, app: DeployedApp) -> bool:
        ok = await _probe_once(app.port)
        app.last_probe_ok = ok
        app.last_probe_at = datetime.now(timezone.utc)
        if app.process is not None and app.process.poll() is not None and app.status == "running":
            app.status = "error"
            app.error = f"Static server exited (code {app.process.returncode})"
        return ok

    async def _stop_locked(self, app: DeployedApp) -> None:
        proc = app.process
        if proc and proc.poll() is None:
            try:
                if sys.platform == "win32":
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    proc.terminate()
                for _ in range(50):
                    if proc.poll() is not None:
                        break
                    await asyncio.sleep(0.1)
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
            except Exception:
                logger.exception("Error stopping process for %s", app.app_id)
        await port_pool.release(app.port)
        app.status = "stopped"
        app.process = None


def _spawn_log_pump(proc: subprocess.Popen, log: AppLogStore) -> None:
    """Drain subprocess stdout into the log store on a background thread."""
    def pump():
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                log.append(line)
        except Exception:
            pass
    t = threading.Thread(target=pump, daemon=True, name=f"logpump-{log.app_id}")
    t.start()


async def _probe_once(port: int) -> bool:
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            resp = await client.get(f"http://127.0.0.1:{port}/")
            return resp.status_code < 500
    except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError):
        return False


async def _wait_for_ready(port: int, timeout: int) -> bool:
    for _ in range(timeout * 4):
        if await _probe_once(port):
            return True
        await asyncio.sleep(0.25)
    return False


registry = AppRegistry()
