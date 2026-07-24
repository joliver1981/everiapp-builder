"""Lifespan shutdown must terminate — the aiosqlite cancellation wedge.

The bug this locks out: main.py's lifespan used to fire-and-forget ``.cancel()``
on its background loops without awaiting them, leaving asyncio.Runner.close()'s
``_cancel_all_tasks`` to deliver a SECOND cancellation during loop teardown.
That second cancel could interrupt SQLAlchemy's shielded connection-terminate
mid-close; aiosqlite's worker thread then exited on its stop sentinel with
futures still queued behind it, and the abandoned close awaited one of those
futures forever. Result: ``TestClient.__exit__`` (and real service stops —
nssm only masks it by killing after its grace period) hung nondeterministically
with an aiosqlite ``_connection_worker_thread`` still alive. Reproduced
reliably within ~40 fast lifespan cycles before the fix.

The fix (main.py lifespan): cancel AND await the loop tasks while the event
loop is healthy, then ``engine.dispose()`` so no pooled aiosqlite connection
(each owns a worker thread) outlives the lifespan.

The cycle loop must run in a SUBPROCESS: a wedged event loop can't time itself
out, so the timeout has to come from outside. The child arms a faulthandler
watchdog per cycle so a wedge produces thread stacks in the failure output
instead of a silent kill.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_TMP = Path(tempfile.gettempdir()) / "aihub-integration" / "lifespan_shutdown"

_CYCLES = 20
_PER_CYCLE_WATCHDOG_S = 60  # one wedged cycle dumps stacks + exits(1)
_TOTAL_TIMEOUT_S = 300      # backstop for the whole child process

_CHILD_SCRIPT = r"""
import faulthandler
import os
import sys
import threading
import time
from pathlib import Path

tmp = Path(sys.argv[1])
cycles = int(sys.argv[2])
watchdog_s = int(sys.argv[3])

tmp.mkdir(parents=True, exist_ok=True)
db = tmp / "lifespan.db"
if db.exists():
    db.unlink()

os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db}"
os.environ["APP_DATA_DIR"] = str(tmp / "apps")
os.environ["DEBUG"] = "true"
os.environ["MASTER_ENCRYPTION_KEY"] = "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8="
os.environ["JWT_SECRET_KEY"] = "lifespan-shutdown-test"

from fastapi.testclient import TestClient
from src.main import app
from src.tracing.writer import span_writer

for i in range(1, cycles + 1):
    # Re-armed every cycle: any single enter/exit that wedges dumps all
    # thread stacks to stderr and hard-exits(1).
    faulthandler.dump_traceback_later(watchdog_s, exit=True)
    with TestClient(app) as client:
        if i % 5 == 0:
            # Exercise a real request path (session checkout/checkin) so
            # shutdown also runs with recently-used pool connections.
            r = client.get("/api/health")
            assert r.status_code == 200, r.text
        if i % 7 == 0:
            # Give the span writer a queued row so its shutdown flush does
            # real DB work during lifespan teardown.
            span_writer.enqueue({
                "trace_id": "t" * 8, "kind": "ai.call",
                "name": f"lifespan-test-{i}", "status": "ok",
            })
    print(f"cycle {i}/{cycles} ok", flush=True)

faulthandler.cancel_dump_traceback_later()

# engine.dispose() in lifespan shutdown must reap every aiosqlite worker
# thread — a lingering one is exactly the pre-fix symptom. Grace loop: the
# worker exits shortly after its stop sentinel is processed.
deadline = time.monotonic() + 10
while time.monotonic() < deadline:
    workers = [t for t in threading.enumerate()
               if "_connection_worker_thread" in (t.name or "")]
    if not workers:
        break
    time.sleep(0.1)
else:
    print(f"LEAKED aiosqlite worker threads: {workers}", flush=True)
    sys.exit(2)

print("ALL CYCLES CLEAN", flush=True)
"""


def test_lifespan_enter_exit_cycles_terminate():
    proc = subprocess.run(
        [
            sys.executable, "-c", _CHILD_SCRIPT,
            str(_TMP), str(_CYCLES), str(_PER_CYCLE_WATCHDOG_S),
        ],
        cwd=str(_BACKEND_DIR),
        capture_output=True,
        text=True,
        timeout=_TOTAL_TIMEOUT_S,
    )
    tail = "\n".join((proc.stdout + "\n" + proc.stderr).strip().splitlines()[-40:])
    assert proc.returncode == 0, (
        f"lifespan cycle child exited {proc.returncode} "
        f"(1 = a cycle wedged, stacks below; 2 = leaked aiosqlite threads):\n{tail}"
    )
    assert "ALL CYCLES CLEAN" in proc.stdout, tail
