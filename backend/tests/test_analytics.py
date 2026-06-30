"""Per-app usage analytics: event recording + admin aggregates."""
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
_DB = _TMP / "test_analytics.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_analytics")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "analytics-test")

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


def _insert_app(app_id: str):
    db_path = settings.database_url[len("sqlite+aiosqlite:///"):]
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT id FROM users WHERE username='admin' LIMIT 1").fetchone()
        conn.execute(
            "INSERT OR IGNORE INTO apps (id, name, description, icon, status, current_version, "
            "ai_toggle_enabled, bug_widget_enabled, bug_fix_auto_approve_max_risk, "
            "ai_verify_level, ai_verify_max_iterations, created_by, created_at, updated_at) "
            "VALUES (?, ?, '', 'app-window', 'published', 1, 0, 0, 'none', 'tsc_build_boot', 8, ?, "
            "datetime('now'), datetime('now'))",
            (app_id, f"ana-{app_id[:8]}", row[0]),
        )
        conn.commit()
    finally:
        conn.close()


def test_record_and_aggregate(client, admin_token, dev_token):
    app_id = str(uuid.uuid4())
    _insert_app(app_id)

    # Admin records 2 launches + 1 view; developer records 1 launch
    for _ in range(2):
        assert client.post(f"/api/apps/{app_id}/events", json={"event_type": "launch"},
                           headers=_auth(admin_token)).status_code == 204
    assert client.post(f"/api/apps/{app_id}/events",
                       json={"event_type": "view", "metadata": {"path": "/reports"}},
                       headers=_auth(admin_token)).status_code == 204
    assert client.post(f"/api/apps/{app_id}/events", json={"event_type": "launch"},
                       headers=_auth(dev_token)).status_code == 204

    r = client.get(f"/api/admin/apps/{app_id}/analytics?days=30", headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["total_events"] == 4
    assert a["unique_users"] == 2  # admin + developer
    assert a["by_type"]["launch"] == 3
    assert a["by_type"]["view"] == 1
    assert len(a["by_day"]) >= 1
    assert "llm_cost_usd" in a


def test_top_apps_leaderboard(client, admin_token):
    app_id = str(uuid.uuid4())
    _insert_app(app_id)
    for _ in range(5):
        client.post(f"/api/apps/{app_id}/events", json={"event_type": "launch"}, headers=_auth(admin_token))

    r = client.get("/api/admin/analytics/top-apps?days=30&limit=50", headers=_auth(admin_token))
    assert r.status_code == 200
    rows = {row["app_id"]: row for row in r.json()}
    assert app_id in rows
    assert rows[app_id]["events"] >= 5
    assert rows[app_id]["name"].startswith("ana-")


def test_developer_can_read_app_analytics_but_not_leaderboard(client, dev_token):
    app_id = str(uuid.uuid4())
    _insert_app(app_id)
    client.post(f"/api/apps/{app_id}/events", json={"event_type": "launch"}, headers=_auth(dev_token))
    # App-scoped analytics: allowed for developers
    assert client.get(f"/api/admin/apps/{app_id}/analytics", headers=_auth(dev_token)).status_code == 200
    # Global leaderboard: admin only
    assert client.get("/api/admin/analytics/top-apps", headers=_auth(dev_token)).status_code in (401, 403)
