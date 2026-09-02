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
    """Client-correctable problem. Maps to 4xx/5xx with a fixable message.
    `logs` carries the function's stderr tail when the failure happened
    inside the function (so a test run can show what it printed)."""

    def __init__(self, message: str, status_code: int = 400, logs: list[str] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.logs = logs or []


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
    user, token_payload: dict | None, trigger: str = "app",
) -> dict:
    """Run one server function. Returns {ok, result, logs, duration_ms}.
    Raises FunctionError for client-correctable problems.

    `trigger` is "app" for the SDK's callFunction and "panel" for a developer
    test run from the Functions panel; both are recorded in the call log."""
    started = time.monotonic()
    outcome = "error"
    source = "?"
    call_ok = False
    call_status = 200
    call_error = ""
    call_result = None
    call_logs: list[str] = []
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
        call_logs = logs
        envelope = _parse_envelope(out_b)
        if envelope is None:
            tail = err_b.decode("utf-8", errors="replace").strip()[-300:]
            raise FunctionError(
                f"Server function runner produced no result (exit {rc})"
                f"{': ' + tail if tail else ''}", status_code=502, logs=logs)

        if not envelope.get("ok"):
            err = envelope.get("error") or {}
            msg = str(err.get("message", "unknown error"))
            trace = str(err.get("trace", "")).strip()
            if trace:
                logger.info("server fn %s/%s error trace:\n%s", app.id, name, trace)
            raise FunctionError(f"Server function '{name}' failed: {msg}",
                                status_code=400, logs=logs)

        result = envelope.get("result")
        outcome = "ok"
        call_ok = True
        call_result = result
        return {
            "ok": True,
            "result": result,
            "logs": logs,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    except FunctionError as e:
        outcome = f"{e.status_code} {str(e)[:120]}"
        call_status = e.status_code
        call_error = str(e)
        raise
    finally:
        duration_ms = int((time.monotonic() - started) * 1000)
        db.add(AuditLog(
            user_id=user.id, action="app_function.call",
            resource_type="app_function", resource_id=f"{app.id}/{name}",
            details=f"app={app.id} fn={name} src={source} -> {outcome} {duration_ms}ms",
        ))
        await record_call(
            db, app_id=app.id, fn_name=name, source=source, user_id=user.id,
            trigger=trigger, ok=call_ok, status_code=call_status, error=call_error,
            duration_ms=duration_ms, args=args, result=call_result, logs=call_logs,
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Call log — the Functions panel's "recent calls"
# ---------------------------------------------------------------------------

_PREVIEW_CHARS = 2000
_LOGS_CHARS = 8 * 1024
_CALLS_KEPT_PER_APP = 300


def _preview(value) -> str:
    if value is None:
        return ""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= _PREVIEW_CHARS else text[:_PREVIEW_CHARS] + "…"


async def record_call(
    db: AsyncSession, *, app_id: str, fn_name: str, source: str, user_id: str,
    trigger: str, ok: bool, status_code: int, error: str, duration_ms: int,
    args, result, logs: list[str],
) -> None:
    """Append to the per-app call log and keep it bounded. Never raises — a
    logging failure must not turn a successful invocation into an error."""
    from sqlalchemy import delete, func, select
    from .models import FunctionCall
    try:
        joined = "\n".join(logs or [])
        db.add(FunctionCall(
            app_id=app_id, fn_name=fn_name, source=source, user_id=user_id or "",
            trigger=trigger, ok=ok, status_code=status_code, error=(error or "")[:2000],
            duration_ms=duration_ms, args_preview=_preview(args),
            result_preview=_preview(result) if ok else "",
            logs=joined[-_LOGS_CHARS:],
        ))
        await db.flush()
        count = (await db.execute(
            select(func.count()).select_from(FunctionCall).where(FunctionCall.app_id == app_id)
        )).scalar_one()
        if count > _CALLS_KEPT_PER_APP:
            cutoff = (await db.execute(
                select(FunctionCall.created_at)
                .where(FunctionCall.app_id == app_id)
                .order_by(FunctionCall.created_at.desc())
                .offset(_CALLS_KEPT_PER_APP).limit(1)
            )).scalar_one_or_none()
            if cutoff is not None:
                await db.execute(delete(FunctionCall).where(
                    FunctionCall.app_id == app_id, FunctionCall.created_at <= cutoff))
    except Exception:
        logger.exception("could not record function call %s/%s", app_id, fn_name)


async def list_calls(db: AsyncSession, app_id: str, fn_name: str | None = None,
                     limit: int = 50) -> list[dict]:
    from sqlalchemy import select
    from .models import FunctionCall
    stmt = select(FunctionCall).where(FunctionCall.app_id == app_id)
    if fn_name:
        stmt = stmt.where(FunctionCall.fn_name == fn_name)
    rows = (await db.execute(
        stmt.order_by(FunctionCall.created_at.desc()).limit(max(1, min(limit, 200)))
    )).scalars().all()
    return [{
        "id": r.id, "fn_name": r.fn_name, "source": r.source, "trigger": r.trigger,
        "ok": r.ok, "status_code": r.status_code, "error": r.error,
        "duration_ms": r.duration_ms, "args_preview": r.args_preview,
        "result_preview": r.result_preview, "logs": r.logs,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]


# ---------------------------------------------------------------------------
# Developer view — what the Functions panel lists, and scaffolding
# ---------------------------------------------------------------------------

# Both SDK entry points — callFunction('name', args) and the useFunction('name')
# hook — optionally with a TypeScript generic in between, which generated code
# often spreads over several lines (useFunction<{ ok: boolean; ... }>('name')).
_CALL_SITE_RE = re.compile(
    r"(?:callFunction|useFunction)\s*(?:<[^()]*?>)?\s*\(\s*['\"`]([a-z][a-z0-9_-]{0,63})['\"`]",
    re.DOTALL)
_SRC_SUFFIXES = (".ts", ".tsx", ".js", ".jsx")


def _draft_dir(app) -> Path:
    return (Path(settings.app_data_dir) / app.id).resolve() / "draft" / "frontend"


def _published_dir(app) -> Path | None:
    v = int(app.current_version or 0)
    if v <= 0:
        return None
    vd = (Path(settings.app_data_dir) / app.id).resolve() / "versions" / f"v{v}"
    return vd if vd.is_dir() else None


def _summary_of(source_text: str) -> str:
    """First line of the module docstring (or the handler's) — the panel's
    one-line description. Empty when the function has neither."""
    import ast
    try:
        tree = ast.parse(source_text)
    except SyntaxError:
        return ""
    doc = ast.get_docstring(tree)
    if not doc:
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "handler":
                doc = ast.get_docstring(node)
                break
    if not doc:
        return ""
    return doc.strip().splitlines()[0].strip()[:200]


def _call_sites(draft_dir: Path) -> dict[str, list[str]]:
    """Which UI files call which function — a static cross-reference over
    src/, so the panel can show 'used by App.tsx' and flag orphans."""
    sites: dict[str, list[str]] = {}
    src = draft_dir / "src"
    if not src.is_dir():
        return sites
    for path in src.rglob("*"):
        if not path.is_file() or path.suffix not in _SRC_SUFFIXES:
            continue
        if "sdk" in path.relative_to(src).parts[:1]:
            continue  # the vendored SDK defines callFunction; it doesn't call one
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(path.relative_to(draft_dir)).replace("\\", "/")
        for m in _CALL_SITE_RE.finditer(text):
            sites.setdefault(m.group(1), [])
            if rel not in sites[m.group(1)]:
                sites[m.group(1)].append(rel)
    return sites


def describe_functions(app) -> list[dict]:
    """The DRAFT tree's server functions with everything the panel shows:
    timeout, one-line summary, size, last modified, UI call sites, and whether
    the current published version has the function too."""
    draft = _draft_dir(app)
    fdir = _functions_dir(draft)
    if not fdir.is_dir():
        return []
    published = _published_dir(app)
    sites = _call_sites(draft)
    out = []
    for f in sorted(fdir.iterdir()):
        if not f.is_file() or f.suffix not in RUNTIMES or not _FN_NAME_RE.match(f.stem):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
            stat = f.stat()
        except OSError:
            continue
        out.append({
            "name": f.stem,
            "runtime": RUNTIMES[f.suffix],
            "path": f"server/functions/{f.name}",
            "timeout_s": _extract_timeout(text),
            "summary": _summary_of(text),
            "size_bytes": stat.st_size,
            "modified_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(stat.st_mtime)) + "Z",
            "callers": sites.get(f.stem, []),
            "in_published": bool(published and _find_fn_file(published, f.stem)),
        })
    return out


_SCAFFOLD = '''"""{title}

One-line summary shown in the Functions panel — replace this.
"""
from sdk import Ctx

CONFIG = {{"timeout_s": 30}}  # optional; literal only, max 120


def handler(args, ctx: Ctx):
    """Called by the app's UI as callFunction('{name}', args)."""
    ctx.log("{name} called with", args)
    # rows = ctx.db.query("SELECT * FROM my_table", limit=50_000)["rows"]
    return {{"ok": True, "echo": args}}
'''


def scaffold_function(app, name: str) -> str:
    """Create server/functions/<name>.py from the starter template.
    Returns the app-relative path. Raises FunctionError on a bad name or if
    the function already exists."""
    if not _FN_NAME_RE.match(name or ""):
        raise FunctionError(
            "Function names are lowercase letters, digits, '-' or '_', starting with a "
            "letter (max 64 chars) — e.g. 'analyze-source'.", status_code=400)
    draft = _draft_dir(app)
    if _find_fn_file(draft, name):
        raise FunctionError(f"A server function named '{name}' already exists.", status_code=409)
    fdir = _functions_dir(draft)
    fdir.mkdir(parents=True, exist_ok=True)
    title = name.replace("-", " ").replace("_", " ").strip().capitalize()
    (fdir / f"{name}.py").write_text(_SCAFFOLD.format(name=name, title=title), encoding="utf-8")
    return f"server/functions/{name}.py"


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
