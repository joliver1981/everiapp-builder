import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

# Make `src` importable as `src.…`
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Use a temp database + temp app data dir for tests so we never touch the real one.
_TMP = Path(tempfile.gettempdir()) / "aihub-tests"
_TMP.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_TMP / 'test.db'}")
os.environ.setdefault("APP_DATA_DIR", str(_TMP / "apps"))
os.environ.setdefault("DEBUG", "true")
# Valid 32-byte url-safe-base64 Fernet key (only used for tests)
os.environ.setdefault(
    "MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8="
)
# NOTE: POST /api/apps no longer copies the template's ~140MB node_modules at
# scaffold time (it cost ~40s per app creation and once dominated this suite's
# runtime). Dependencies are provisioned lazily by preview start / the AI
# verifier via src/apps/provisioning.py — no test starts a real vite process,
# so the suite never pays it.


@pytest.fixture
def tmp_app_dir(tmp_path):
    return tmp_path


# --- aiosqlite worker-thread drain -------------------------------------------
# engine.dispose() only QUEUES each pooled connection's close — it does NOT
# join the connection's worker thread. When a pytest-asyncio function-scoped
# event loop closes before that thread exits, sqlite3's C objects get
# finalized cross-thread, which intermittently kills the whole suite with
# "Windows fatal exception: access violation" (seen 2026-07-24 at ~47%
# progress under heavy machine load; faulthandler showed the pytest-asyncio
# finalizer closing the loop with an aiosqlite _connection_worker_thread
# alive). Any fixture that creates its own async engine must depend on
# `aiosqlite_drain` FIRST so teardown waits for the workers it spawned.

def _aiosqlite_worker_idents() -> set[int]:
    return {
        t.ident for t in threading.enumerate()
        if "_connection_worker_thread" in (t.name or "")
    }


def drain_aiosqlite_workers(baseline: frozenset[int] | set[int] = frozenset(),
                            timeout: float = 5.0) -> None:
    """Wait (bounded, best-effort) for aiosqlite worker threads beyond
    `baseline` to exit. Never raises: a stuck thread should surface in the
    next crash dump, not hang teardown."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not (_aiosqlite_worker_idents() - set(baseline)):
            return
        time.sleep(0.005)


@pytest.fixture
def aiosqlite_drain():
    """Depend on this BEFORE creating a per-test async engine:

        @pytest.fixture
        async def db(aiosqlite_drain):
            engine = create_async_engine(...)
            ...
            await engine.dispose()

    Setup snapshots the worker threads that already exist (e.g. the global
    engine's pool); teardown runs AFTER the depending fixture's dispose and
    BEFORE pytest-asyncio closes the test's event loop, and waits only for
    the NEW workers — the ones this test's engine spawned — to exit."""
    baseline = _aiosqlite_worker_idents()
    yield
    drain_aiosqlite_workers(baseline)
