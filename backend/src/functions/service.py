"""App-authored server functions: Python files under server/functions/ in the
app's own tree, executed in a child interpreter ON the platform host.

The filesystem is the registry — a function IS its file (name = filename stem,
runtime = extension), so version snapshots, rollback, diff, and marketplace
packaging cover functions with zero extra machinery, and there is no second
source of truth to drift. The child process gets an app-scoped token and makes
its ctx.* calls back through the platform's existing app-facing HTTP routes,
inheriting their gates, rate limits, size caps, and audit logging.

Windows constraint (same as the runtime manager / verifier): uvicorn's
SelectorEventLoop can't spawn asyncio subprocesses, so the child runs via
subprocess.run inside asyncio.to_thread with a hard timeout.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import sys
import time
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from .. import python_env
from ..config import settings
from ..secrets.models import AuditLog

logger = logging.getLogger(__name__)

# Extension → runtime. Adding a runtime later (e.g. ".ts" → "node") is one
# entry here plus one harness — the route/SDK/registry are runtime-agnostic.
RUNTIMES = {".py": "python"}

_FN_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
# CONFIG = {"timeout_s": N} must be a literal — the parent reads it from source
# text (regex, not import) because the timeout bounds the very process that
# would evaluate the file.
_TIMEOUT_RE = re.compile(
    r"CONFIG\s*=\s*\{[^}]*[\"']timeout_s[\"']\s*:\s*(\d+)", re.DOTALL)

DEFAULT_TIMEOUT_S = 30
MAX_TIMEOUT_S = 120
MAX_RESULT_BYTES = 5 * 1024 * 1024
_LOG_TAIL_BYTES = 8 * 1024
_LOG_TAIL_LINES = 100
_SENTINEL = "AIHUB_FN_RESULT:"


class FunctionError(Exception):
    """Client-correctable problem. Maps to 4xx/5xx with a fixable message."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def resolve_fn_dir(app, token_payload: dict | None) -> tuple[Path, str]:
    """Which app tree an invocation executes from.

    Preview tokens (the builder's iframe) run the draft — that's what the
    developer is iterating on. Everyone else (embed viewers, deployed apps
    phoning home, plain login sessions) runs the published version so a
    version stays immutable end-to-end; draft only when never published.
    """
    # Absolute from the start: settings.app_data_dir is RELATIVE in dev
    # ("./data/apps"), and the child process runs with cwd=source_dir — a
    # relative fn path handed to it would re-resolve against that cwd.
    base = (Path(settings.app_data_dir) / app.id).resolve()
    purpose = (token_payload or {}).get("purpose")
    if purpose == "preview":
        return base / "draft" / "frontend", "draft"
    v = int(app.current_version or 0)
    if v > 0:
        vd = base / "versions" / f"v{v}"
        if vd.is_dir():
            return vd, f"v{v}"
    return base / "draft" / "frontend", "draft"


def _functions_dir(source_dir: Path) -> Path:
    return source_dir / "server" / "functions"


def _find_fn_file(source_dir: Path, name: str) -> Path | None:
    for ext in RUNTIMES:
        cand = _functions_dir(source_dir) / f"{name}{ext}"
        if cand.is_file():
            return cand
    return None


def _extract_timeout(source_text: str) -> int:
    m = _TIMEOUT_RE.search(source_text)
    if not m:
        return DEFAULT_TIMEOUT_S
    return max(1, min(int(m.group(1)), MAX_TIMEOUT_S))


def list_functions(app, token_payload: dict | None) -> list[dict]:
    """The app's server functions, from the tree this caller would execute."""
    source_dir, _ = resolve_fn_dir(app, token_payload)
    fdir = _functions_dir(source_dir)
    if not fdir.is_dir():
        return []
    out = []
    for f in sorted(fdir.iterdir()):
        if not f.is_file() or f.suffix not in RUNTIMES:
            continue
        if not _FN_NAME_RE.match(f.stem):
            continue
        try:
            timeout_s = _extract_timeout(f.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        out.append({"name": f.stem, "runtime": RUNTIMES[f.suffix], "timeout_s": timeout_s})
    return out


def _harness_path() -> Path:
    if getattr(sys, "frozen", False):
        # Bundled as a PyInstaller data file — the CHILD interpreter runs it,
        # so it can't live inside the frozen archive's importable modules.
        return Path(getattr(sys, "_MEIPASS")) / "functions_runner" / "harness.py"
    return Path(__file__).parent / "runner" / "harness.py"


def _child_env() -> dict:
    """Whitelist — the platform's env (MASTER_ENCRYPTION_KEY, JWT secret, DB
    URL, ...) must never reach AI-generated code. PATH is included because the
    dev interpreter may be a conda env whose numpy/pandas need its DLL dirs;
    it carries no secrets."""
    import os
    keep = ("SystemRoot", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT",
            "TEMP", "TMP", "PATH", "WINDIR")
    return {k: os.environ[k] for k in keep if k in os.environ}


def _not_found_error(app, name: str, source: str) -> FunctionError:
    # If the function exists in draft but the caller executes a published
    # version, the fix is publishing — say so instead of a generic 404.
    if source != "draft":
        draft_dir = Path(settings.app_data_dir) / app.id / "draft" / "frontend"
        if _find_fn_file(draft_dir, name):
            return FunctionError(
                f"Server function '{name}' is not in this app's published version "
                f"({source}) — publish a new version to include it.", status_code=404)
    return FunctionError(
        f"This app has no server function named '{name}'. Functions live in "
        f"server/functions/<name>.py — ask the AI builder to create one.",
        status_code=404)


async def invoke_function(
    db: AsyncSession, *, app, name: str, args, token: str, base_url: str,
    user, token_payload: dict | None,
) -> dict:
    """Run one server function. Returns {ok, result, logs, duration_ms}.
    Raises FunctionError for client-correctable problems."""
    started = time.monotonic()
    outcome = "error"
    source = "?"
    try:
        source_dir, source = resolve_fn_dir(app, token_payload)
        if not _FN_NAME_RE.match(name or ""):
            raise _not_found_error(app, name, source)
        fn_file = _find_fn_file(source_dir, name)
        if not fn_file:
            raise _not_found_error(app, name, source)

        py = python_env.python_cmd()
        if not py:
            raise FunctionError(
                "The platform's Python runtime for server functions is not "
                "available — reinstall the platform or set AIHUB_PYTHON_DIR.",
                status_code=503)

        source_text = fn_file.read_text(encoding="utf-8", errors="replace")
        timeout_s = _extract_timeout(source_text)

        # Admin-installed packages (Admin → Python Packages) — the harness puts
        # this on the child's sys.path after the app's server/ dirs. Called
        # through the module so tests can monkeypatch the resolver.
        managed_dir = python_env.managed_packages_dir()
        payload = json.dumps({
            "args": args,
            "meta": {
                "app_id": app.id,
                "base_url": base_url,
                # Rides stdin (with the rest of the payload), never env/argv —
                # a token in argv would show in process listings.
                "token": token,
                "user": {"id": user.id, "username": user.username},
                "fn_name": name,
                "timeout_s": timeout_s,
                "extra_sys_path": [str(managed_dir)] if managed_dir.is_dir() else [],
            },
        }).encode("utf-8")

        cmd = [py, "-B", "-s", str(_harness_path()), str(fn_file)]
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0

        def _run():
            try:
                cp = subprocess.run(
                    cmd, input=payload, capture_output=True,
                    timeout=timeout_s + 5, cwd=str(source_dir),
                    env=_child_env(), creationflags=creationflags,
                )
                return cp.returncode, cp.stdout or b"", cp.stderr or b""
            except subprocess.TimeoutExpired:
                return 124, b"", b""

        rc, out_b, err_b = await asyncio.to_thread(_run)

        if rc == 124:
            raise FunctionError(
                f"Server function '{name}' exceeded its {timeout_s}s timeout and "
                "was terminated. Raise CONFIG = {\"timeout_s\": ...} (max "
                f"{MAX_TIMEOUT_S}) or reduce the work per call.", status_code=504)

        logs = _log_tail(err_b)
        envelope = _parse_envelope(out_b)
        if envelope is None:
            tail = err_b.decode("utf-8", errors="replace").strip()[-300:]
            raise FunctionError(
                f"Server function runner produced no result (exit {rc})"
                f"{': ' + tail if tail else ''}", status_code=502)

        if not envelope.get("ok"):
            err = envelope.get("error") or {}
            msg = str(err.get("message", "unknown error"))
            trace = str(err.get("trace", "")).strip()
            if trace:
                logger.info("server fn %s/%s error trace:\n%s", app.id, name, trace)
            raise FunctionError(f"Server function '{name}' failed: {msg}", status_code=400)

        result = envelope.get("result")
        outcome = "ok"
        return {
            "ok": True,
            "result": result,
            "logs": logs,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    except FunctionError as e:
        outcome = f"{e.status_code} {str(e)[:120]}"
        raise
    finally:
        duration_ms = int((time.monotonic() - started) * 1000)
        db.add(AuditLog(
            user_id=user.id, action="app_function.call",
            resource_type="app_function", resource_id=f"{app.id}/{name}",
            details=f"app={app.id} fn={name} src={source} -> {outcome} {duration_ms}ms",
        ))
        await db.commit()


def _parse_envelope(stdout_bytes: bytes) -> dict | None:
    """Last sentinel line wins — survives stray writes to the real stdout."""
    if len(stdout_bytes) > MAX_RESULT_BYTES + 65536:
        # Belt-and-braces with the harness-side cap.
        return {"ok": False, "error": {"message": "function output exceeded the 5 MiB cap"}}
    text = stdout_bytes.decode("utf-8", errors="replace")
    for line in reversed(text.splitlines()):
        if line.startswith(_SENTINEL):
            try:
                return json.loads(line[len(_SENTINEL):])
            except json.JSONDecodeError:
                return None
    return None


def _log_tail(stderr_bytes: bytes) -> list[str]:
    text = stderr_bytes[-_LOG_TAIL_BYTES:].decode("utf-8", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[-_LOG_TAIL_LINES:]
