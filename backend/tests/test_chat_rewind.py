"""Chat undo/rewind: draft-history ring buffer + HTTP endpoints."""
from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_chat_rewind.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_chat_rewind")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "chat-rewind-test")

from src.ai import snapshots  # noqa: E402
from src.config import settings  # noqa: E402
from src.database import init_db  # noqa: E402
from src.main import app  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _init():
    asyncio.run(init_db())
    yield


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin_token(client):
    return client.post("/api/auth/login", json={"username": "admin", "password": "password"}).json()["access_token"]


@pytest.fixture(scope="module")
def dev_token(client):
    return client.post("/api/auth/login", json={"username": "developer", "password": "password"}).json()["access_token"]


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


def _draft_file(app_id: str) -> Path:
    p = Path(settings.app_data_dir) / app_id / "draft" / "frontend" / "src" / "App.tsx"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def test_history_push_list_restore():
    app_id = str(uuid.uuid4())
    f = _draft_file(app_id)

    f.write_text("v1", encoding="utf-8")
    s1 = snapshots.history_push(app_id, note="turn one")
    f.write_text("v2", encoding="utf-8")
    s2 = snapshots.history_push(app_id, note="turn two")
    assert s1 == 1 and s2 == 2

    entries = snapshots.history_list(app_id)
    assert [e["seq"] for e in entries] == [2, 1]   # newest first
    assert entries[1]["note"] == "turn one"

    # Mutate, then rewind to seq 1 → file is "v1" again
    f.write_text("v3-unsaved", encoding="utf-8")
    assert snapshots.history_restore(app_id, 1) is True
    assert f.read_text(encoding="utf-8") == "v1"

    # The rewind captured the pre-rewind state as a new entry (so it's undoable)
    assert any("before rewind" in e["note"] for e in snapshots.history_list(app_id))


def test_history_restore_missing_returns_false():
    app_id = str(uuid.uuid4())
    _draft_file(app_id).write_text("x", encoding="utf-8")
    assert snapshots.history_restore(app_id, 999) is False


def test_history_ring_buffer_caps():
    app_id = str(uuid.uuid4())
    f = _draft_file(app_id)
    for i in range(snapshots.MAX_HISTORY + 5):
        f.write_text(f"v{i}", encoding="utf-8")
        snapshots.history_push(app_id, note=f"t{i}")
    entries = snapshots.history_list(app_id)
    assert len(entries) <= snapshots.MAX_HISTORY


def test_http_history_endpoints(client, admin_token):
    app_id = str(uuid.uuid4())
    f = _draft_file(app_id)
    f.write_text("alpha", encoding="utf-8")
    snapshots.history_push(app_id, note="first")
    f.write_text("beta", encoding="utf-8")

    r = client.get(f"/api/apps/{app_id}/history", headers=_auth(admin_token))
    assert r.status_code == 200
    entries = r.json()["entries"]
    assert len(entries) >= 1
    seq = entries[-1]["seq"]

    r = client.post(f"/api/apps/{app_id}/history/{seq}/restore", headers=_auth(admin_token))
    assert r.status_code == 200 and r.json()["ok"] is True
    assert f.read_text(encoding="utf-8") == "alpha"

    # missing seq → 404
    assert client.post(f"/api/apps/{app_id}/history/9999/restore",
                       headers=_auth(admin_token)).status_code == 404


def test_history_requires_developer(client):
    user = client.post("/api/auth/login", json={"username": "user", "password": "password"}).json()["access_token"]
    assert client.get(f"/api/apps/{uuid.uuid4()}/history", headers=_auth(user)).status_code in (401, 403)
