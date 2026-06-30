"""TestClient integration test for GET /api/apps/{id}/runtime/status.

Confirms the new phase / phase_detail / phase_elapsed_seconds fields are
populated and round-trip through the API correctly. This is what the
frontend polls every 1.5s to show live progress in the loading spinner.
"""
import asyncio
import os
import tempfile
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_AIHUB_TESTS_TMP = Path(tempfile.gettempdir()) / "aihub-tests"
for _candidate in (
    _TMP / "test_runtime_status.db",
    _AIHUB_TESTS_TMP / "test.db",
):
    if _candidate.exists():
        try:
            _candidate.unlink()
        except OSError:
            pass

_DB = _TMP / "test_runtime_status.db"
_APPS_DIR = _TMP / "apps_runtime_status"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_APPS_DIR)
os.environ["DEBUG"] = "true"
os.environ.setdefault(
    "MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8="
)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

from src.config import settings  # noqa: E402
from src.main import app  # noqa: E402
from src.runtime.manager import runtime_manager  # noqa: E402

settings.app_data_dir = str(_APPS_DIR)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin_token(client: TestClient) -> str:
    r = client.post("/api/auth/login", json={"username": "admin", "password": "password"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(t: str) -> dict:
    return {"Authorization": f"Bearer {t}"}


def test_status_stopped_when_never_started(client: TestClient, admin_token: str):
    app_id = str(uuid.uuid4())
    # Don't even create the app — runtime/status returns stopped for any unknown id
    r = client.get(f"/api/apps/{app_id}/runtime/status", headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "stopped"
    # No phase fields when nothing's running
    assert body.get("phase") is None
    assert body.get("phase_detail") is None


def test_status_exposes_phase_during_start(client: TestClient, admin_token: str, monkeypatch):
    """Start a runtime with a stub do_start that sets phase='installing' and holds.
    Confirm the HTTP status reflects that phase + a sensible elapsed time."""
    app_id = str(uuid.uuid4())
    # Create an app row so the start endpoint's get_app() check passes
    r = client.post("/api/apps", json={"name": f"phase-{app_id[:8]}"}, headers=_auth(admin_token))
    assert r.status_code in (200, 201), r.text
    app_id = r.json()["id"]
    # Seed the draft dir so _do_start (real one) doesn't immediately error
    (Path(settings.app_data_dir) / app_id / "draft" / "frontend").mkdir(parents=True, exist_ok=True)

    # Stub _do_start so we don't actually npm install / spawn vite.
    async def fake_do_start(app_proc, source):
        runtime_manager._set_phase(app_proc, "installing", "fake npm install")
        await asyncio.sleep(3)  # hold long enough that the test polls during it
        app_proc.status = "running"
        runtime_manager._set_phase(app_proc, "running", "fake ready")
    monkeypatch.setattr(runtime_manager, "_do_start", fake_do_start)

    try:
        r = client.post(
            f"/api/apps/{app_id}/runtime/start",
            json={"source": "draft"},
            headers=_auth(admin_token),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # The POST returns IMMEDIATELY now (no waiting for npm install)
        assert body["status"] == "starting"
        assert body["phase"] in ("queued", "installing")  # whichever the worker has set by now

        # Give the background task a tick to start
        time.sleep(0.5)

        # Now GET should show 'installing'
        r = client.get(f"/api/apps/{app_id}/runtime/status", headers=_auth(admin_token))
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "starting"
        assert body["phase"] == "installing"
        assert "fake npm install" in (body.get("phase_detail") or "")
        # phase_elapsed_seconds is reported and reasonable
        assert body.get("phase_elapsed_seconds") is not None
        assert 0 <= body["phase_elapsed_seconds"] < 5

        # After the fake do_start finishes, status -> running
        time.sleep(3)
        r = client.get(f"/api/apps/{app_id}/runtime/status", headers=_auth(admin_token))
        body = r.json()
        assert body["status"] == "running"
        assert body["phase"] == "running"
    finally:
        # Clean up: stop the runtime so subsequent tests aren't polluted
        client.post(f"/api/apps/{app_id}/runtime/stop", headers=_auth(admin_token))
