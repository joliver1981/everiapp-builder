"""Code-review approval workflow: submit -> review -> approve/reject.

When `require_publish_approval` is on, developers cannot publish directly; they
submit a request that an admin approves (performing the real publish) or rejects.
The toggle is reset in fixture teardown so the shared test engine/DB doesn't leak
the setting into other test files.
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
_DB = _TMP / "test_publish_approval.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_publish_approval")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "publish-approval-test")

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


@pytest.fixture
def approval_required(client, admin_token):
    """Turn the approval gate on for one test, then reset it (shared DB hygiene)."""
    client.put("/api/admin/settings", json={"require_publish_approval": True}, headers=_auth(admin_token))
    yield
    client.put("/api/admin/settings", json={"require_publish_approval": False}, headers=_auth(admin_token))


def _new_clean_app(admin_username_present: bool = True) -> str:
    app_id = str(uuid.uuid4())
    # Insert the app row owned by admin
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
            (app_id, f"appr-{app_id[:8]}", row[0]),
        )
        conn.commit()
    finally:
        conn.close()
    # Clean draft (no security findings)
    p = Path(settings.app_data_dir) / app_id / "draft" / "frontend" / "src" / "App.tsx"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("export default function App(){return <div>ok</div>}", encoding="utf-8")
    return app_id


def test_direct_publish_when_approval_off(client, admin_token, dev_token):
    """With the gate off (default), a developer can publish directly."""
    app_id = _new_clean_app()
    r = client.post(f"/api/apps/{app_id}/versions", json={"notes": "direct"}, headers=_auth(dev_token))
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 1


def test_developer_blocked_then_request_approved(client, admin_token, dev_token, approval_required):
    app_id = _new_clean_app()

    # Developer can no longer publish directly
    r = client.post(f"/api/apps/{app_id}/versions", json={"notes": "x"}, headers=_auth(dev_token))
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "approval_required"

    # Developer submits a request
    r = client.post(f"/api/apps/{app_id}/publish-requests", json={"notes": "please ship"},
                    headers=_auth(dev_token))
    assert r.status_code == 201, r.text
    req = r.json()
    assert req["status"] == "pending"
    req_id = req["id"]

    # It shows up in the global admin queue
    r = client.get("/api/admin/publish-requests", headers=_auth(admin_token))
    assert any(x["id"] == req_id for x in r.json())

    # Admin approves → real publish happens, app advances to v1
    r = client.post(f"/api/apps/{app_id}/publish-requests/{req_id}/approve",
                    json={"review_note": "LGTM"}, headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "approved"
    assert body["resulting_version"] == 1
    assert body["reviewed_by"]

    # The version really exists
    r = client.get(f"/api/apps/{app_id}/versions", headers=_auth(admin_token))
    assert any(v["version"] == 1 for v in r.json())


def test_admin_can_reject(client, admin_token, dev_token, approval_required):
    app_id = _new_clean_app()
    r = client.post(f"/api/apps/{app_id}/publish-requests", json={"notes": "v"}, headers=_auth(dev_token))
    req_id = r.json()["id"]

    r = client.post(f"/api/apps/{app_id}/publish-requests/{req_id}/reject",
                    json={"review_note": "needs tests"}, headers=_auth(admin_token))
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"
    assert r.json()["review_note"] == "needs tests"

    # No version was created
    r = client.get(f"/api/apps/{app_id}/versions", headers=_auth(admin_token))
    assert r.json() == []

    # Re-approving a rejected request is refused
    r = client.post(f"/api/apps/{app_id}/publish-requests/{req_id}/approve",
                    json={}, headers=_auth(admin_token))
    assert r.status_code == 409


def test_admin_can_still_publish_directly_under_approval(client, admin_token, approval_required):
    """Admins are the reviewers — they keep the direct-publish path."""
    app_id = _new_clean_app()
    r = client.post(f"/api/apps/{app_id}/versions", json={"notes": "admin direct"},
                    headers=_auth(admin_token))
    assert r.status_code == 201, r.text


def test_publish_policy_off_by_default(client, dev_token):
    """With the gate off, the builder is told it can publish directly."""
    app_id = _new_clean_app()
    r = client.get(f"/api/apps/{app_id}/publish-policy", headers=_auth(dev_token))
    assert r.status_code == 200, r.text
    assert r.json()["require_approval"] is False


def test_publish_policy_on_for_dev_bypassed_for_admin(client, admin_token, dev_token, approval_required):
    """Gate on: developers must request approval; admins (reviewers) do not."""
    app_id = _new_clean_app()
    r = client.get(f"/api/apps/{app_id}/publish-policy", headers=_auth(dev_token))
    assert r.status_code == 200, r.text
    assert r.json()["require_approval"] is True

    r = client.get(f"/api/apps/{app_id}/publish-policy", headers=_auth(admin_token))
    assert r.json()["require_approval"] is False


def test_developer_cannot_approve(client, dev_token, approval_required):
    app_id = _new_clean_app()
    r = client.post(f"/api/apps/{app_id}/publish-requests", json={"notes": "v"}, headers=_auth(dev_token))
    req_id = r.json()["id"]
    # Developer trying to approve their own request → 403 (admin-only)
    r = client.post(f"/api/apps/{app_id}/publish-requests/{req_id}/approve",
                    json={}, headers=_auth(dev_token))
    assert r.status_code in (401, 403)
