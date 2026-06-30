"""Audit-log search + system-status dashboard."""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_observability.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_observability")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "observability-test")

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


def _insert_audit(action: str, details: str, resource_type: str = "test"):
    db_path = settings.database_url[len("sqlite+aiosqlite:///"):]
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT id FROM users WHERE username='admin' LIMIT 1").fetchone()
        conn.execute(
            "INSERT INTO audit_logs (id, user_id, action, resource_type, resource_id, details, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
            (str(uuid.uuid4()), row[0], action, resource_type, "r1", details),
        )
        conn.commit()
    finally:
        conn.close()


def test_audit_search_filters(client, admin_token):
    _insert_audit("obs.alpha", "ordinary event")
    _insert_audit("obs.beta", "contains the needle keyword")
    _insert_audit("obs.alpha", "another alpha", resource_type="widget")

    r = client.get("/api/admin/audit-logs", headers=_auth(admin_token))
    assert r.status_code == 200
    assert r.json()["total"] >= 3
    assert r.json()["items"]  # newest first
    # username resolved
    assert any(i["username"] == "admin" for i in r.json()["items"])

    # action prefix filter
    r = client.get("/api/admin/audit-logs?action=obs.alpha", headers=_auth(admin_token))
    assert all(i["action"] == "obs.alpha" for i in r.json()["items"])
    assert r.json()["total"] == 2

    # free-text
    r = client.get("/api/admin/audit-logs?q=needle", headers=_auth(admin_token))
    assert r.json()["total"] == 1
    assert "needle" in r.json()["items"][0]["details"]

    # resource_type
    r = client.get("/api/admin/audit-logs?resource_type=widget", headers=_auth(admin_token))
    assert r.json()["total"] == 1

    # pagination
    r = client.get("/api/admin/audit-logs?limit=1", headers=_auth(admin_token))
    assert len(r.json()["items"]) == 1 and r.json()["limit"] == 1


def test_audit_actions_list(client, admin_token):
    r = client.get("/api/admin/audit-logs/actions", headers=_auth(admin_token))
    assert r.status_code == 200
    actions = {a["action"] for a in r.json()}
    assert "obs.alpha" in actions


def test_audit_search_admin_only(client, dev_token):
    assert client.get("/api/admin/audit-logs", headers=_auth(dev_token)).status_code in (401, 403)


def test_system_status(client, admin_token):
    r = client.get("/api/admin/system/status", headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["version"]
    assert "uptime_seconds" in s
    assert s["counts"]["users"] >= 1
    assert "apps" in s["counts"]
    assert "background_loops" in s and "health_probe" in s["background_loops"]
    assert "database" in s and "disk" in s


def test_system_status_admin_only(client, dev_token):
    assert client.get("/api/admin/system/status", headers=_auth(dev_token)).status_code in (401, 403)
