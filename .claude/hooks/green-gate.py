"""green-gate — Stop hook that blocks turn-end when tests or typecheck are red.

Runs (in this order, stopping at the first failure):
  1. backend pytest         (~5 min — the integration-heavy part)
  2. aihub-agent pytest     (~3s)
  3. frontend tsc -b        (~15s)

Exit codes:
  0  — all green, turn may end
  2  — at least one check failed; output is shown to Claude, who must fix and retry

Skips checks for stacks that aren't installed (e.g. missing venv → backend skipped
with a warning, not a failure — so the gate doesn't deadlock fresh clones).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


# --- Locate things ----------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]
VENV_PY = ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
TSC = ROOT / "frontend" / "node_modules" / ".bin" / ("tsc.cmd" if os.name == "nt" else "tsc")


# --- ANSI colors (best-effort; the hook output usually goes to stderr) ------

def _c(s: str, code: str) -> str:
    return f"\x1b[{code}m{s}\x1b[0m" if sys.stderr.isatty() else s


GREEN = lambda s: _c(s, "32")  # noqa: E731
RED = lambda s: _c(s, "31")    # noqa: E731
DIM = lambda s: _c(s, "2")     # noqa: E731


# --- Check runners ----------------------------------------------------------

class Check:
    def __init__(self, name: str, cmd: list[str], cwd: Path, env: dict | None = None):
        self.name = name
        self.cmd = cmd
        self.cwd = cwd
        self.env = env

    def run(self) -> tuple[bool, str, float]:
        """Returns (passed, combined_output, duration_seconds)."""
        env = {**os.environ, **(self.env or {})}
        t0 = time.monotonic()
        try:
            r = subprocess.run(
                self.cmd,
                cwd=str(self.cwd),
                env=env,
                capture_output=True,
                text=True,
                # 2400s budget: the backend suite spans 5 feature waves plus the
                # marketplace export/import + publish-pipeline + remote-gallery
                # HTTP integration tests (each module spins a full TestClient
                # lifespan). Measured wall time on a QUIET machine was 920s on
                # 2026-07-02 (567 passed / 115 skipped); by 2026-07-06 the suite
                # had grown to 756 items (each new *_http.py module adds ~20-25s
                # of TestClient-lifespan startup) and legitimately runs ~25min,
                # tipping the old 1500s cap into TIMEOUT blocks that looked like
                # failures on a loaded machine. 2026-07-07: root cause of the
                # bloat found — POST /api/apps copied the template's ~140MB
                # node_modules on every test app creation (~40s × dozens of
                # apps). Scaffold no longer copies node_modules at all (deps
                # are provisioned lazily at first preview/verify, see
                # src/apps/provisioning.py) and the suite runs ~296s quiet
                # (652 passed / 116 skipped). 1200s = 4x headroom for loaded
                # machines while still catching real hangs.
                timeout=1200,
            )
        except subprocess.TimeoutExpired as e:
            return False, f"TIMEOUT after {e.timeout}s", time.monotonic() - t0
        duration = time.monotonic() - t0
        out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
        return r.returncode == 0, out.strip(), duration


def _checks() -> list[Check]:
    out: list[Check] = []

    backend_tests = ROOT / "backend" / "tests"
    if VENV_PY.exists() and backend_tests.exists():
        out.append(Check(
            "backend pytest",
            [str(VENV_PY), "-m", "pytest", "-q", "--no-header"],
            cwd=ROOT / "backend",
        ))
    else:
        print(f"[green-gate] skip backend pytest (venv or tests missing)", file=sys.stderr)

    agent_tests = ROOT / "aihub-agent" / "tests"
    if VENV_PY.exists() and agent_tests.exists():
        out.append(Check(
            "aihub-agent pytest",
            [str(VENV_PY), "-m", "pytest", "-q", "--no-header"],
            cwd=ROOT / "aihub-agent",
        ))
    else:
        print(f"[green-gate] skip aihub-agent pytest (venv or tests missing)", file=sys.stderr)

    if TSC.exists():
        out.append(Check(
            "frontend tsc",
            # `tsc -b` (what `npm run build` runs) typechecks via the project
            # references. Bare `tsc --noEmit` on the references-only root
            # config typechecks ZERO files — the gate stayed green while the
            # production build was broken (7 latent errors found 2026-06-12).
            [str(TSC), "-b"],
            cwd=ROOT / "frontend",
        ))
    else:
        print(f"[green-gate] skip frontend tsc ({TSC} missing — run npm install in frontend/)", file=sys.stderr)

    return out


# --- Cross-process lock ------------------------------------------------------
# Two gates at once fight over the same pytest temp DBs (WinError 32) and both
# go red with noise. Overlaps are real: a manual gate in one session vs. the
# Stop hook, or an orphaned pytest child from a harness-killed hook. Serialize:
# second comer waits (bounded), breaks stale locks (dead holder PID), and if
# the other gate is still running at the deadline, defers to it instead of
# corrupting both runs.

LOCK = Path(__file__).resolve().parent / ".green-gate.lock"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True,
        )
        return str(pid) in (r.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _acquire_lock(wait_s: float = 150.0) -> bool:
    deadline = time.monotonic() + wait_s
    announced = False
    while True:
        try:
            fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                holder = int((LOCK.read_text().strip() or "0"))
            except (OSError, ValueError):
                holder = 0
            if not _pid_alive(holder):
                # Dead holder (crashed/killed gate) — break the stale lock.
                try:
                    LOCK.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                return False
            if not announced:
                print(
                    f"[green-gate] another gate is running (pid {holder}); waiting...",
                    file=sys.stderr,
                )
                announced = True
            time.sleep(5)


def _release_lock() -> None:
    try:
        if LOCK.exists() and (LOCK.read_text().strip() or "0") == str(os.getpid()):
            LOCK.unlink()
    except OSError:
        pass


def main() -> int:
    checks = _checks()
    if not checks:
        print("[green-gate] nothing to check (no venv, no tests, no tsc)", file=sys.stderr)
        return 0

    if not _acquire_lock():
        # A live gate is still running the same checks — its verdict stands.
        # Exiting 0 here is deliberate: colliding would wreck BOTH runs.
        print(
            "[green-gate] deferring to the gate already in progress (still running "
            "after the wait budget); its result is authoritative",
            file=sys.stderr,
        )
        return 0
    try:
        return _run_checks(checks)
    finally:
        _release_lock()


def _run_checks(checks: list[Check]) -> int:
    failures: list[tuple[Check, str, float]] = []
    durations: list[tuple[str, float]] = []

    for check in checks:
        ok, output, dur = check.run()
        durations.append((check.name, dur))
        if not ok:
            failures.append((check, output, dur))
            # Stop at first failure — no point burning time on later ones.
            break

    summary = "  ".join(f"{name} {dur:.1f}s" for name, dur in durations)

    if not failures:
        # All green — be silent so we don't add noise to clean turns.
        print(f"[green-gate] {GREEN('green')}  {DIM(summary)}", file=sys.stderr)
        return 0

    # At least one red.
    check, output, dur = failures[0]
    print(
        f"\n[green-gate] {RED('BLOCKED')}: {check.name} failed ({dur:.1f}s)\n"
        f"{DIM('cmd:')} {' '.join(check.cmd)}\n"
        f"{DIM('cwd:')} {check.cwd}\n"
        f"--- output ---\n{output}\n--- end output ---\n"
        f"\nFix the failure above before ending the turn. "
        f"Re-run this gate with: python .claude/hooks/green-gate.py",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
