"""WAVE 2 JOURNEY — governance & lifecycle features working together.

A realistic flow that exercises the whole Wave 2 surface in one story:

  1. A developer builds an app whose draft accidentally contains a hardcoded
     cloud key. The SECURITY SCAN blocks the publish.
  2. They fix it; the publish now succeeds (v1).
  3. They iterate and publish v2; the VERSION DIFF shows exactly what changed.
  4. Users launch the app; PER-APP ANALYTICS records the activity and the app
     appears on the usage leaderboard.
  5. An admin turns on IFRAME EMBEDDING; the framed bootstrap advertises the
     allowed parent via a frame-ancestors CSP.

Integration-level (TestClient), so it runs in the default gate.
"""
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
_DB = _TMP / "test_journey_wave2.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_journey_wave2")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "journey-wave2-test")

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


def _draft(app_id: str) -> Path:
    d = Path(settings.app_data_dir) / app_id / "draft" / "frontend"
    (d / "src").mkdir(parents=True, exist_ok=True)
    return d


def _insert_app(app_id: str):
    db_path = settings.database_url[len("sqlite+aiosqlite:///"):]
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT id FROM users WHERE username='admin' LIMIT 1").fetchone()
        conn.execute(
            "INSERT OR IGNORE INTO apps (id, name, description, icon, status, current_version, "
            "ai_toggle_enabled, bug_widget_enabled, bug_fix_auto_approve_max_risk, "
            "ai_verify_level, ai_verify_max_iterations, created_by, created_at, updated_at) "
            "VALUES (?, ?, '', 'app-window', 'draft', 0, 0, 0, 'none', 'tsc_build_boot', 8, ?, "
            "datetime('now'), datetime('now'))",
            (app_id, f"w2-{app_id[:8]}", row[0]),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.journey
def test_wave2_full_lifecycle(client, admin_token, dev_token):
    app_id = str(uuid.uuid4())
    _insert_app(app_id)
    d = _draft(app_id)

    # 1. Risky draft — hardcoded AWS key → publish blocked by the security scan
    (d / "src" / "App.tsx").write_text(
        'const AWS = "AKIAIOSFODNN7EXAMPLE"\nexport default function App(){return <div>v1</div>}',
        encoding="utf-8",
    )
    r = client.post(f"/api/apps/{app_id}/versions", json={"notes": "v1"}, headers=_auth(admin_token))
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "security_scan_blocked"

    # 2. Fix it → clean publish (v1)
    (d / "src" / "App.tsx").write_text(
        "export default function App(){return <div>v1</div>}", encoding="utf-8")
    r = client.post(f"/api/apps/{app_id}/versions", json={"notes": "v1 clean"}, headers=_auth(admin_token))
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 1

    # 3. Iterate → v2, then diff v1->v2
    (d / "src" / "App.tsx").write_text(
        "export default function App(){return <div>v2 improved</div>}", encoding="utf-8")
    r = client.post(f"/api/apps/{app_id}/versions", json={"notes": "v2"}, headers=_auth(admin_token))
    assert r.json()["version"] == 2

    r = client.get(f"/api/apps/{app_id}/versions/diff?from=1&to=2", headers=_auth(admin_token))
    assert r.status_code == 200
    diff = r.json()
    assert diff["summary"]["modified"] == 1
    appf = next(f for f in diff["files"] if f["path"] == "src/App.tsx")
    assert "v2 improved" in appf["diff"]

    # 4. Users launch the app → analytics records events + leaderboard
    for tok in (admin_token, dev_token, admin_token):
        assert client.post(f"/api/apps/{app_id}/events", json={"event_type": "launch"},
                           headers=_auth(tok)).status_code == 204
    r = client.get(f"/api/admin/apps/{app_id}/analytics?days=30", headers=_auth(admin_token))
    a = r.json()
    assert a["total_events"] == 3
    assert a["unique_users"] == 2
    top = client.get("/api/admin/analytics/top-apps?days=30&limit=50", headers=_auth(admin_token)).json()
    assert any(row["app_id"] == app_id for row in top)

    # 5. Turn on embedding → framed bootstrap advertises the allowed parent
    client.put(f"/api/apps/{app_id}/embed-config",
               json={"enabled": True, "allowed_origins": ["https://intranet.acme.com"]},
               headers=_auth(admin_token))
    r = client.get(f"/api/apps/{app_id}/embed")
    assert r.status_code == 200
    assert "https://intranet.acme.com" in r.headers.get("content-security-policy", "")
