"""Verify a generated app's draft directory: tsc → vite build → boot probe → runtime probe.

This is the same green-gate idea we use for ourselves, applied to AI-generated apps.
Each layer returns a list of structured errors. Empty list = passed.

Layer details + what each catches:

  1. tsc --noEmit    (~3-10s)   Type errors, missing imports, bad refs.
  2. vite build      (~10-30s)  Build-time issues: CSS, assets, plugin failures, dynamic-import resolution.
  3. boot probe      (~5-15s)   Spawns `npx serve` against dist/, GETs `/`, fetches the main JS bundle.
                                Catches "build succeeded but bundle 404s" / "blank page because the wrong file got published".
  4. runtime probe   (~10-20s)  Launches headless Chromium (Playwright) against the served app.
                                Listens for: console.error, window.onerror, unhandledrejection, page crashes.
                                Waits for #root to have children. Catches "compiles + builds + serves
                                but throws on mount" — the broadest class of bugs the AI can ship.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

from ..config import settings
from ..apps.provisioning import try_copy_template_node_modules
from .probe_shared import (
    A11Y_AUDIT_JS as _A11Y_AUDIT_JS,
    MOUNT_TIMEOUT_MS as _RUNTIME_MOUNT_TIMEOUT_MS,
    RUNTIME_IGNORE_SUBSTRINGS as _RUNTIME_IGNORE_SUBSTRINGS,
    is_noise as _is_noise,
)

logger = logging.getLogger(__name__)

# Hard wall-clock cap for the out-of-process runtime probe (npx serve + browser).
_RUNTIME_PROBE_TIMEOUT_S = 60


from .. import node_env

NPM_CMD = node_env.npm_cmd()
NPX_CMD = node_env.npx_cmd()


@dataclass
class VerifyError:
    """One actionable error to feed back to the LLM."""
    stage: str           # "tsc" | "build" | "boot"
    file: str | None     # relative path inside draft/frontend, when known
    line: int | None
    column: int | None
    code: str | None     # error code (e.g. "TS2304"), when known
    message: str         # human-readable text


@dataclass
class VerifyResult:
    """Outcome of one verification pass."""
    stage_reached: str   # "tsc" | "build" | "boot" | "done"
    duration_seconds: float = 0.0
    errors: list[VerifyError] = field(default_factory=list)
    # Optional summary strings shown in the chat panel
    summary: str = ""

    @property
    def passed(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "stage_reached": self.stage_reached,
            "duration_seconds": round(self.duration_seconds, 2),
            "passed": self.passed,
            "summary": self.summary,
            "errors": [asdict(e) for e in self.errors],
        }


# ---------- Helpers ----------


def _app_draft_dir(app_id: str) -> Path:
    return Path(settings.app_data_dir).resolve() / app_id / "draft" / "frontend"


async def _run(
    cmd: list[str],
    cwd: Path,
    timeout: int,
    env: dict | None = None,
) -> tuple[int, str, str, float]:
    """Run a subprocess off the event loop. Returns (returncode, stdout, stderr, duration)."""
    full_env = {**os.environ, **(env or {})}
    t0 = time.monotonic()

    def _go() -> tuple[int, str, str]:
        try:
            r = subprocess.run(
                cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                env=full_env,
                timeout=timeout,
            )
            return r.returncode, r.stdout or "", r.stderr or ""
        except subprocess.TimeoutExpired as e:
            return 124, "", f"TIMEOUT after {e.timeout}s"

    rc, out, err = await asyncio.get_event_loop().run_in_executor(None, _go)
    return rc, out, err, time.monotonic() - t0


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------- Stage 0: npm install if needed ----------


async def ensure_node_modules(app_id: str) -> VerifyError | None:
    """Provision node_modules if missing: template copy first, then npm install.

    Scaffold no longer bundles node_modules (it cost ~40s on POST /api/apps), so
    the first verify of a fresh app provisions it here — the offline template
    copy when the app's declared deps still match, else a real npm install.
    Returns None on success, or a VerifyError describing what went wrong. We treat
    npm-install failures as build errors so the AI can attempt a fix (e.g. by
    correcting a malformed package.json it just wrote).
    """
    app_dir = _app_draft_dir(app_id)
    if not app_dir.exists():
        return VerifyError(stage="build", file=None, line=None, column=None, code=None,
                           message=f"App draft directory missing: {app_dir}")
    if (app_dir / "node_modules").exists():
        return None

    if await try_copy_template_node_modules(app_dir):
        return None

    logger.info("verifier: npm install for %s", app_id)
    rc, out, err, _ = await _run(
        [NPM_CMD, "install", "--no-audit", "--no-fund"],
        cwd=app_dir,
        timeout=180,
    )
    if rc != 0:
        return VerifyError(
            stage="build", file="package.json", line=None, column=None, code=None,
            message=f"npm install failed:\n{(err or out)[:1000]}",
        )
    return None


# ---------- Stage 1: tsc --noEmit ----------

_TSC_LINE_RE = re.compile(
    r"^(?P<file>[^()]+)\((?P<line>\d+),(?P<col>\d+)\):\s+error\s+(?P<code>TS\d+):\s+(?P<msg>.+)$"
)


def _parse_tsc_errors(output: str, app_dir: Path) -> list[VerifyError]:
    errs: list[VerifyError] = []
    for raw_line in output.splitlines():
        m = _TSC_LINE_RE.match(raw_line.strip())
        if not m:
            continue
        # Normalize file path to be relative to draft/frontend
        file_str = m.group("file").strip()
        try:
            file_str = str(Path(file_str).resolve().relative_to(app_dir.resolve())).replace("\\", "/")
        except (ValueError, OSError):
            pass
        errs.append(VerifyError(
            stage="tsc",
            file=file_str,
            line=int(m.group("line")),
            column=int(m.group("col")),
            code=m.group("code"),
            message=m.group("msg"),
        ))
    return errs


async def run_tsc(app_id: str) -> VerifyResult:
    app_dir = _app_draft_dir(app_id)
    t0 = time.monotonic()

    err = await ensure_node_modules(app_id)
    if err is not None:
        return VerifyResult(
            stage_reached="tsc",
            duration_seconds=time.monotonic() - t0,
            errors=[err],
            summary="npm install failed",
        )

    tsc_bin = app_dir / "node_modules" / ".bin" / ("tsc.cmd" if sys.platform == "win32" else "tsc")
    if not tsc_bin.exists():
        # Fallback to npx
        cmd = [NPX_CMD, "tsc", "--noEmit", "--pretty", "false"]
    else:
        cmd = [str(tsc_bin), "--noEmit", "--pretty", "false"]

    rc, out, errstream, dur = await _run(cmd, cwd=app_dir, timeout=120)
    combined = out + ("\n" + errstream if errstream else "")

    if rc == 0:
        return VerifyResult(
            stage_reached="tsc",
            duration_seconds=time.monotonic() - t0,
            summary="tsc clean",
        )

    errors = _parse_tsc_errors(combined, app_dir)
    if not errors:
        # tsc returned non-zero but our regex didn't catch anything — surface raw output
        errors = [VerifyError(
            stage="tsc", file=None, line=None, column=None, code=None,
            message=f"tsc exited {rc}:\n{combined[:1000]}",
        )]
    return VerifyResult(
        stage_reached="tsc",
        duration_seconds=time.monotonic() - t0,
        errors=errors,
        summary=f"tsc found {len(errors)} error{'s' if len(errors) != 1 else ''}",
    )


# ---------- Stage 2: vite build ----------

# Vite errors look like:  `[plugin:vite:react-babel] /path/src/Foo.tsx:12:5: Adjacent JSX...`
# or                      `error during build:\nRollupError: Could not resolve "./missing" from "src/App.tsx"`
_VITE_PATH_RE = re.compile(r"(?P<file>[/\\][^\s:]+\.(?:tsx?|jsx?|css|json))(?::(?P<line>\d+))?(?::(?P<col>\d+))?")


def _parse_build_errors(output: str, app_dir: Path) -> list[VerifyError]:
    errs: list[VerifyError] = []
    # Group lines after "error during build" or "[vite:" markers.
    blocks = re.split(r"(?m)^(?=error during build|\[vite|\[plugin:)", output)
    for block in blocks:
        block = block.strip()
        if not block or ("error" not in block.lower() and "failed" not in block.lower()):
            continue
        m = _VITE_PATH_RE.search(block)
        file_str: str | None = None
        line: int | None = None
        col: int | None = None
        if m:
            try:
                file_str = str(Path(m.group("file")).resolve().relative_to(app_dir.resolve())).replace("\\", "/")
            except (ValueError, OSError):
                file_str = m.group("file")
            if m.group("line"):
                line = int(m.group("line"))
            if m.group("col"):
                col = int(m.group("col"))
        errs.append(VerifyError(
            stage="build", file=file_str, line=line, column=col, code=None,
            message=block[:1500],
        ))
    # De-dup near-identical messages
    seen: set[str] = set()
    unique: list[VerifyError] = []
    for e in errs:
        key = (e.file or "") + e.message[:200]
        if key in seen:
            continue
        seen.add(key)
        unique.append(e)
    return unique


async def run_build(app_id: str) -> VerifyResult:
    app_dir = _app_draft_dir(app_id)
    t0 = time.monotonic()

    err = await ensure_node_modules(app_id)
    if err is not None:
        return VerifyResult(
            stage_reached="build",
            duration_seconds=time.monotonic() - t0,
            errors=[err],
            summary="npm install failed",
        )

    rc, out, errstream, _ = await _run(
        [NPM_CMD, "run", "build"],
        cwd=app_dir,
        timeout=180,
    )
    combined = out + ("\n" + errstream if errstream else "")

    if rc == 0:
        return VerifyResult(
            stage_reached="build",
            duration_seconds=time.monotonic() - t0,
            summary="build clean",
        )

    errors = _parse_build_errors(combined, app_dir)
    if not errors:
        errors = [VerifyError(
            stage="build", file=None, line=None, column=None, code=None,
            message=f"vite build exited {rc}:\n{combined[:1500]}",
        )]
    return VerifyResult(
        stage_reached="build",
        duration_seconds=time.monotonic() - t0,
        errors=errors,
        summary=f"build failed: {len(errors)} error{'s' if len(errors) != 1 else ''}",
    )


# ---------- Stage 3: boot probe ----------

async def _probe_url(url: str, timeout_s: float = 2.0) -> tuple[bool, str]:
    """Return (ok, detail)."""
    try:
        async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as c:
            r = await c.get(url)
            if r.status_code >= 400:
                return False, f"HTTP {r.status_code}"
            return True, f"HTTP {r.status_code} ({len(r.content)} bytes)"
    except httpx.HTTPError as e:
        return False, f"{type(e).__name__}: {e}"


async def run_boot_probe(app_id: str) -> VerifyResult:
    """Serve dist/ on a free port, GET / and the main JS bundle. Kill the server."""
    app_dir = _app_draft_dir(app_id)
    dist = app_dir / "dist"
    t0 = time.monotonic()

    if not dist.exists():
        return VerifyResult(
            stage_reached="boot",
            duration_seconds=time.monotonic() - t0,
            errors=[VerifyError(
                stage="boot", file=None, line=None, column=None, code=None,
                message="dist/ does not exist — did vite build succeed?",
            )],
            summary="no dist/",
        )

    index = dist / "index.html"
    if not index.exists():
        return VerifyResult(
            stage_reached="boot",
            duration_seconds=time.monotonic() - t0,
            errors=[VerifyError(
                stage="boot", file="index.html", line=None, column=None, code=None,
                message="dist/index.html missing after build",
            )],
            summary="no index.html",
        )

    port = _free_port()
    # `npx --yes serve` works on most setups. We use it not `vite preview` because
    # `serve` is faster to spin up and doesn't load the vite plugin chain.
    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        [NPX_CMD, "--yes", "serve", "-s", "dist", "-l", str(port), "--no-clipboard"],
        cwd=str(app_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=creation_flags,
    )
    errors: list[VerifyError] = []

    try:
        # Wait up to 12s for the server to bind.
        ready = False
        for _ in range(48):
            ok, _ = await _probe_url(f"http://127.0.0.1:{port}/", timeout_s=1.0)
            if ok:
                ready = True
                break
            if proc.poll() is not None:
                break
            await asyncio.sleep(0.25)

        if not ready:
            stderr_blob = ""
            try:
                if proc.stdout:
                    stderr_blob = proc.stdout.read(2000).decode(errors="replace")
            except Exception:
                pass
            errors.append(VerifyError(
                stage="boot", file=None, line=None, column=None, code=None,
                message=f"static server failed to bind on port {port} within 12s.\n{stderr_blob}",
            ))
        else:
            # 1. Index must load
            ok, detail = await _probe_url(f"http://127.0.0.1:{port}/")
            if not ok:
                errors.append(VerifyError(
                    stage="boot", file="index.html", line=None, column=None, code=None,
                    message=f"GET / returned {detail}",
                ))
            else:
                # 2. Pull the main JS bundle out of index.html and confirm it loads.
                html = index.read_text(encoding="utf-8", errors="replace")
                bundle = _extract_main_bundle_url(html)
                if bundle:
                    ok, detail = await _probe_url(f"http://127.0.0.1:{port}{bundle}")
                    if not ok:
                        errors.append(VerifyError(
                            stage="boot", file=None, line=None, column=None, code=None,
                            message=f"Main JS bundle {bundle} returned {detail}",
                        ))
    finally:
        try:
            import signal
            if sys.platform == "win32":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.terminate()
            for _ in range(20):
                if proc.poll() is not None:
                    break
                await asyncio.sleep(0.1)
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        except Exception:
            pass

    if errors:
        return VerifyResult(
            stage_reached="boot",
            duration_seconds=time.monotonic() - t0,
            errors=errors,
            summary=f"boot probe failed: {len(errors)} error{'s' if len(errors) != 1 else ''}",
        )
    return VerifyResult(
        stage_reached="boot",
        duration_seconds=time.monotonic() - t0,
        summary="boot probe ok",
    )


def _extract_main_bundle_url(html: str) -> str | None:
    """Find the first `<script type="module" src="/assets/...">` URL."""
    m = re.search(r'<script[^>]*\bsrc="(/[^"]+\.js)"', html)
    return m.group(1) if m else None


# ---------- Stage 4: runtime probe (Playwright) ----------

# The noise filter, mount timeout, and a11y audit JS live in probe_shared (shared
# verbatim with runtime_probe_child.py) and are imported at the top of this file
# as _RUNTIME_IGNORE_SUBSTRINGS / _RUNTIME_MOUNT_TIMEOUT_MS / _is_noise / _A11Y_AUDIT_JS.


# ---------- Accessibility audit ----------
# The audit JS (_A11Y_AUDIT_JS) lives in probe_shared and is imported above;
# _a11y_findings_to_errors maps its raw findings to VerifyError objects.


def _a11y_findings_to_errors(raw: list[dict] | None) -> list[VerifyError]:
    """Map the in-page audit's raw findings to VerifyError objects (stage='a11y')."""
    errors: list[VerifyError] = []
    for f in (raw or []):
        rule = (f.get("rule") or "a11y") if isinstance(f, dict) else "a11y"
        detail = (f.get("detail") or "accessibility issue") if isinstance(f, dict) else str(f)
        sel = (f.get("selector") or "") if isinstance(f, dict) else ""
        msg = f"a11y [{rule}]: {detail}" + (f" — {sel}" if sel else "")
        errors.append(VerifyError(
            stage="a11y", file=None, line=None, column=None, code=rule, message=msg,
        ))
    return errors


def _build_runtime_result(errors: list, probe_crash: str | None,
                          duration: float, app_id: str) -> "VerifyResult":
    """Assemble the runtime VerifyResult.

    A probe-INFRA crash (`probe_crash`) is NON-FATAL: if no real app errors were
    detected, the app passes on tsc/build/boot rather than failing on something
    the LLM can't fix (a crashed/timed-out Playwright probe). Real app errors
    (page errors, console errors, blank #root) always take precedence and fail.
    """
    dedup: list = []
    seen: set[str] = set()
    for e in errors:
        key = e.message[:300]
        if key in seen:
            continue
        seen.add(key)
        dedup.append(e)
    if dedup:
        return VerifyResult(
            stage_reached="runtime", duration_seconds=duration, errors=dedup,
            summary=f"runtime: {len(dedup)} issue{'s' if len(dedup) != 1 else ''}",
        )
    if probe_crash:
        logger.warning("runtime probe could not run for %s (%s) — passing on tsc/build/boot",
                       app_id, probe_crash)
        return VerifyResult(
            stage_reached="runtime", duration_seconds=duration,
            summary=f"runtime check skipped — probe couldn't run ({probe_crash[:140]})",
        )
    return VerifyResult(stage_reached="runtime", duration_seconds=duration, summary="runtime clean")


def _parse_child_output(stdout: str) -> dict | None:
    """Parse the probe child's single JSON result line. None if none was found."""
    for line in (stdout or "").splitlines():
        s = line.strip()
        if s.startswith("{"):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                continue
    return None


def _collect_runtime_errors(data: dict, url: str) -> list[VerifyError]:
    """Turn the child's RAW observations into VerifyError objects.

    Keeps all error-shaping in one place: strip dev-tooling noise, synthesize a
    'blank page' error when nothing mounted and nothing threw, map a11y findings,
    then surface page / console / network errors.
    """
    errors: list[VerifyError] = []
    mounted = bool(data.get("mounted"))
    page_errors = [m for m in (data.get("page_errors") or []) if not _is_noise(m)]
    console_errors = [m for m in (data.get("console_errors") or []) if not _is_noise(m)]
    failed_requests = list(data.get("failed_requests") or [])

    if not mounted and not page_errors and not console_errors:
        errors.append(VerifyError(
            stage="runtime", file="src/main.tsx", line=None, column=None, code=None,
            message=(
                f"App loaded but #root has no children after "
                f"{_RUNTIME_MOUNT_TIMEOUT_MS}ms. Likely a mount error swallowed "
                f"silently, or the app renders nothing. Check that <App/> returns "
                f"valid JSX and that all top-level imports resolve."
            ),
        ))

    errors.extend(_a11y_findings_to_errors(data.get("a11y_raw")))

    for msg in page_errors:
        errors.append(VerifyError(
            stage="runtime", file=None, line=None, column=None, code=None, message=msg,
        ))
    for msg in console_errors:
        errors.append(VerifyError(
            stage="runtime", file=None, line=None, column=None, code=None,
            message=f"console.error: {msg}",
        ))
    for msg in failed_requests:
        errors.append(VerifyError(
            stage="runtime", file=None, line=None, column=None, code=None,
            message=f"network failure: {msg}",
        ))
    return errors


async def run_runtime_probe(app_id: str, run_a11y: bool = False) -> VerifyResult:
    """Spawn the static server, launch Chromium, and check that the app actually runs.

    Catches things the boot probe can't:
      - "TypeError: Cannot read properties of undefined (reading 'map')"
      - "Hooks can only be called inside the body of a function component"
      - Anything thrown synchronously during mount
      - Anything thrown asynchronously inside a useEffect / promise
      - Pages that load but never render (#root stays empty)
    """
    app_dir = _app_draft_dir(app_id)
    dist = app_dir / "dist"
    t0 = time.monotonic()

    if not dist.exists():
        return VerifyResult(
            stage_reached="runtime",
            duration_seconds=time.monotonic() - t0,
            errors=[VerifyError(
                stage="runtime", file=None, line=None, column=None, code=None,
                message="dist/ does not exist — vite build must run before runtime probe",
            )],
            summary="no dist/",
        )

    # The actual browser probe runs OUT OF PROCESS (runtime_probe_child.py).
    # A missing Playwright/Chromium comes back from the child as a non-fatal
    # probe_crash (verify passes on tsc/build/boot), not a hard failure.
    port = _free_port()
    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP
    server_proc = subprocess.Popen(
        [NPX_CMD, "--yes", "serve", "-s", "dist", "-l", str(port), "--no-clipboard"],
        cwd=str(app_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=creation_flags,
    )

    errors: list[VerifyError] = []
    probe_crash: str | None = None  # infra crash (Playwright/serve/timeout), NOT an app error
    try:
        # Wait for the static server to bind.
        ready = False
        for _ in range(48):
            ok, _ = await _probe_url(f"http://127.0.0.1:{port}/", timeout_s=1.0)
            if ok:
                ready = True
                break
            if server_proc.poll() is not None:
                break
            await asyncio.sleep(0.25)

        if not ready:
            return VerifyResult(
                stage_reached="runtime",
                duration_seconds=time.monotonic() - t0,
                errors=[VerifyError(
                    stage="runtime", file=None, line=None, column=None, code=None,
                    message=f"static server failed to bind on port {port}",
                )],
                summary="server bind failed",
            )

        # Drive the browser in a SEPARATE PROCESS. uvicorn's Windows
        # SelectorEventLoop can't spawn Chromium (a bare NotImplementedError); a
        # fresh interpreter under asyncio.run gets a Proactor loop where it can.
        # Launched via Popen/run (NOT an asyncio subprocess) so the parent's loop
        # policy is irrelevant, and BY FILE PATH so the child resolves regardless
        # of how this package was imported (backend.src.ai vs src.ai).
        url = f"http://127.0.0.1:{port}/"
        child_py = Path(__file__).with_name("runtime_probe_child.py")
        cmd = [sys.executable, str(child_py), url, "1" if run_a11y else "0"]

        def _run_child():
            try:
                cp = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=_RUNTIME_PROBE_TIMEOUT_S,
                )
                return cp.returncode, cp.stdout or "", cp.stderr or ""
            except subprocess.TimeoutExpired:
                return 124, "", f"runtime probe exceeded {_RUNTIME_PROBE_TIMEOUT_S}s"

        rc, child_out, child_err = await asyncio.to_thread(_run_child)
        data = _parse_child_output(child_out)
        if data is None:
            probe_crash = (
                f"probe child returned no result (rc={rc}): "
                f"{(child_err or child_out or '').strip()[:200]}"
            )
        elif data.get("probe_crash"):
            probe_crash = str(data["probe_crash"])
        else:
            errors = _collect_runtime_errors(data, url)
    except Exception as e:
        # The probe itself failed to RUN (Playwright launch, the static server, a
        # teardown error, a timeout) — infrastructure, NOT an app bug the LLM can
        # fix. Record it (with a diagnosable type, since some exceptions str() to
        # ""), but DON'T turn it into a failing app error.
        logger.exception("runtime probe crashed for %s", app_id)
        detail = str(e).strip()
        probe_crash = f"{type(e).__name__}: {detail}" if detail else type(e).__name__
    finally:
        try:
            import signal
            if sys.platform == "win32":
                server_proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                server_proc.terminate()
            for _ in range(20):
                if server_proc.poll() is not None:
                    break
                await asyncio.sleep(0.1)
            if server_proc.poll() is None:
                server_proc.kill()
                server_proc.wait()
        except Exception:
            pass

    return _build_runtime_result(errors, probe_crash, time.monotonic() - t0, app_id)


# ---------- Top-level orchestrator ----------

VERIFY_LEVELS = (
    "off", "tsc", "tsc_build", "tsc_build_boot",
    "tsc_build_boot_runtime", "tsc_build_boot_runtime_a11y",
)


async def verify_app(app_id: str, level: str, runtime_enabled: bool = True) -> VerifyResult:
    """Run the configured verification stages in order, stopping at the first red one.

    `runtime_enabled` is the platform-wide admin master switch for the headless
    runtime probe (resolved from settings by the caller). When False, the runtime
    stage is skipped even if `level` requests it.
    """
    if level == "off" or level not in VERIFY_LEVELS:
        return VerifyResult(stage_reached="done", summary="verification disabled")

    t0 = time.monotonic()

    tsc = await run_tsc(app_id)
    if not tsc.passed:
        tsc.duration_seconds = time.monotonic() - t0
        return tsc
    if level == "tsc":
        tsc.duration_seconds = time.monotonic() - t0
        return tsc

    build = await run_build(app_id)
    build.duration_seconds = time.monotonic() - t0
    if not build.passed:
        return build
    if level == "tsc_build":
        return build

    boot = await run_boot_probe(app_id)
    boot.duration_seconds = time.monotonic() - t0
    if not boot.passed:
        return boot
    if level == "tsc_build_boot":
        return boot

    # The runtime probe (headless browser) is gated by a platform-wide admin
    # switch, resolved by the caller. When off, we stop here cleanly: the app
    # has still passed type-check + build + boot.
    if not runtime_enabled:
        boot.summary = "runtime check disabled by admin — passed tsc + build + boot"
        return boot

    runtime = await run_runtime_probe(
        app_id, run_a11y=(level == "tsc_build_boot_runtime_a11y"),
    )
    runtime.duration_seconds = time.monotonic() - t0
    return runtime


# ---------- Fix prompt formatting ----------

def errors_to_prompt_block(errors: list[VerifyError]) -> str:
    """Format errors for inclusion in a follow-up LLM prompt."""
    if not errors:
        return ""
    lines: list[str] = ["The previous patch failed verification with these errors:"]
    for i, e in enumerate(errors, 1):
        loc = ""
        if e.file:
            loc = f" {e.file}"
            if e.line is not None:
                loc += f":{e.line}"
            if e.column is not None:
                loc += f":{e.column}"
        code = f" [{e.code}]" if e.code else ""
        lines.append(f"\n{i}. ({e.stage}){loc}{code}")
        lines.append(f"   {e.message.strip()[:1500]}")
    lines.append(
        "\nProduce a corrected patch using the same file-block format. "
        "Output ONLY the files that need to change."
    )
    return "\n".join(lines)
