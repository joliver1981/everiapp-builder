"""TestClient tests for GET /api/apps/{id}/versions/diff.

Publishes two versions that differ (a modified file, an added file, a removed
file) and asserts the unified diff reflects all three, plus version-vs-draft and
error cases.
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
_DB = _TMP / "test_version_diff.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_version_diff")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "version-diff-test")

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


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


def _draft(app_id: str) -> Path:
    d = Path(settings.app_data_dir) / app_id / "draft" / "frontend"
    d.mkdir(parents=True, exist_ok=True)
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
            (app_id, f"diff-{app_id[:8]}", row[0]),
        )
        conn.commit()
    finally:
        conn.close()


def test_version_diff_reports_changes(client, admin_token):
    app_id = str(uuid.uuid4())
    _insert_app(app_id)
    d = _draft(app_id)
    (d / "src").mkdir(exist_ok=True)
    (d / "src" / "App.tsx").write_text("export default function App(){ return <div>one</div> }", encoding="utf-8")
    (d / "src" / "Old.tsx").write_text("export const Old = 1", encoding="utf-8")
    (d / "package.json").write_text('{"name":"diffapp","version":"1"}', encoding="utf-8")

    r = client.post(f"/api/apps/{app_id}/versions", json={"notes": "v1"}, headers=_auth(admin_token))
    assert r.status_code == 201, r.text

    # Mutate the draft: modify App.tsx, add New.tsx, remove Old.tsx
    (d / "src" / "App.tsx").write_text("export default function App(){ return <div>two</div> }", encoding="utf-8")
    (d / "src" / "New.tsx").write_text("export const New = 2", encoding="utf-8")
    (d / "src" / "Old.tsx").unlink()

    r = client.post(f"/api/apps/{app_id}/versions", json={"notes": "v2"}, headers=_auth(admin_token))
    assert r.status_code == 201, r.text

    # Diff v1 -> v2
    r = client.get(f"/api/apps/{app_id}/versions/diff?from=1&to=2", headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"] == {"added": 1, "removed": 1, "modified": 1}
    by_path = {f["path"]: f for f in body["files"]}
    assert by_path["src/New.tsx"]["status"] == "added"
    assert by_path["src/Old.tsx"]["status"] == "removed"
    appf = by_path["src/App.tsx"]
    assert appf["status"] == "modified"
    assert appf["additions"] >= 1 and appf["deletions"] >= 1
    assert "two" in appf["diff"] and "one" in appf["diff"]


def test_diff_version_vs_draft(client, admin_token):
    app_id = str(uuid.uuid4())
    _insert_app(app_id)
    d = _draft(app_id)
    (d / "src").mkdir(exist_ok=True)
    (d / "src" / "App.tsx").write_text("const v = 1", encoding="utf-8")
    r = client.post(f"/api/apps/{app_id}/versions", json={"notes": "v1"}, headers=_auth(admin_token))
    assert r.status_code == 201

    # Change the draft WITHOUT publishing
    (d / "src" / "App.tsx").write_text("const v = 99", encoding="utf-8")

    r = client.get(f"/api/apps/{app_id}/versions/diff?from=1&to=draft", headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"]["modified"] == 1
    assert "99" in body["files"][0]["diff"]


def test_diff_error_cases(client, admin_token):
    app_id = str(uuid.uuid4())
    _insert_app(app_id)
    _draft(app_id)
    # Non-numeric ref → 400
    r = client.get(f"/api/apps/{app_id}/versions/diff?from=abc&to=1", headers=_auth(admin_token))
    assert r.status_code == 400
    # Missing version → 404
    r = client.get(f"/api/apps/{app_id}/versions/diff?from=1&to=2", headers=_auth(admin_token))
    assert r.status_code == 404
