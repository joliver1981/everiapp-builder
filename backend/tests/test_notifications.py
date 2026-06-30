"""Email/SMTP notifications: send gating, test endpoint, smtp_password scrub,
and the publish-request → email wiring.

The SMTP transport (_smtp_send) is monkeypatched to capture messages, so no real
mail server is needed; the compose/gate/recipient logic is what's under test.
"""
from __future__ import annotations

import asyncio
import email
import os
import sqlite3
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_notifications.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_notifications")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "notifications-test")

from src.config import settings  # noqa: E402
from src.database import async_session, init_db  # noqa: E402
from src.main import app  # noqa: E402
from src.notifications import service as notify_service  # noqa: E402


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
def captured(monkeypatch):
    box: list[dict] = []

    def fake(host, port, use_tls, username, password, from_addr, to_addrs, msg_bytes):
        m = email.message_from_bytes(msg_bytes)
        box.append({"host": host, "port": port, "password": password, "from": from_addr,
                    "to": to_addrs, "subject": m["Subject"]})

    monkeypatch.setattr(notify_service, "_smtp_send", fake)
    return box


def _enable_smtp(client, admin_token, **extra):
    payload = {"smtp_enabled": True, "smtp_host": "localhost", "smtp_port": 25,
               "smtp_use_tls": False, "notify_from": "aihub@corp.com",
               "notify_admin_emails": "ops@corp.com", **extra}
    client.put("/api/admin/settings", json=payload, headers=_auth(admin_token))


def _reset(client, admin_token):
    client.put("/api/admin/settings",
               json={"smtp_enabled": False, "require_publish_approval": False},
               headers=_auth(admin_token))


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
            (app_id, f"notif-{app_id[:8]}", row[0]),
        )
        conn.commit()
    finally:
        conn.close()


def test_test_email_endpoint(client, admin_token, captured):
    _enable_smtp(client, admin_token)
    r = client.post("/api/admin/notifications/test", json={"to": "person@corp.com"}, headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert len(captured) == 1
    assert captured[0]["to"] == ["person@corp.com"]
    assert "test email" in captured[0]["subject"].lower()
    _reset(client, admin_token)


def test_disabled_smtp_returns_502(client, admin_token):
    client.put("/api/admin/settings", json={"smtp_enabled": False}, headers=_auth(admin_token))
    r = client.post("/api/admin/notifications/test", json={"to": "x@y.com"}, headers=_auth(admin_token))
    assert r.status_code == 502


def test_smtp_password_scrubbed_and_preserved(client, admin_token, captured):
    # Set a password → GET must not leak it.
    client.put("/api/admin/settings", json={
        "smtp_enabled": True, "smtp_host": "localhost", "smtp_port": 25, "smtp_use_tls": False,
        "smtp_username": "u", "smtp_password": "hunter2", "notify_from": "a@b.com",
    }, headers=_auth(admin_token))
    got = client.get("/api/admin/settings", headers=_auth(admin_token)).json()
    assert got["smtp_password"] == "***REDACTED***"

    # Sending uses the REAL stored password.
    client.post("/api/admin/notifications/test", json={"to": "x@y.com"}, headers=_auth(admin_token))
    assert captured[-1]["password"] == "hunter2"

    # PUT the redacted placeholder back (e.g. user changed other fields) → password preserved.
    client.put("/api/admin/settings", json={"smtp_password": "***REDACTED***", "smtp_username": "u2"},
               headers=_auth(admin_token))
    client.post("/api/admin/notifications/test", json={"to": "x@y.com"}, headers=_auth(admin_token))
    assert captured[-1]["password"] == "hunter2"
    _reset(client, admin_token)


def test_publish_request_emails_admins(client, admin_token, dev_token, captured):
    _enable_smtp(client, admin_token, require_publish_approval=True, notify_on_publish_request=True)
    app_id = str(uuid.uuid4())
    _insert_app(app_id)
    r = client.post(f"/api/apps/{app_id}/publish-requests", json={"notes": "ship it"}, headers=_auth(dev_token))
    assert r.status_code == 201, r.text
    subjects = [c["subject"] for c in captured]
    assert any("Publish request" in s for s in subjects)
    assert any(c["to"] == ["ops@corp.com"] for c in captured)
    _reset(client, admin_token)


def test_bug_report_notification_fires(client, admin_token, captured):
    _enable_smtp(client, admin_token, notify_on_bug_report=True)
    app_id = str(uuid.uuid4())
    _insert_app(app_id)

    async def _run():
        async with async_session() as db:
            await notify_service.notify_bug_report(db, app_id, "Crash on save\n\nstack trace")
    asyncio.run(_run())
    assert any("Bug report" in c["subject"] for c in captured)
    _reset(client, admin_token)


def test_budget_exceeded_notification_throttled(client, admin_token, captured):
    _enable_smtp(client, admin_token, notify_on_budget=True)
    uid = str(uuid.uuid4())  # unique → fresh throttle window

    async def _run():
        async with async_session() as db:
            await notify_service.notify_budget_exceeded(db, uid, "Org monthly budget of $100 exceeded")
            await notify_service.notify_budget_exceeded(db, uid, "second attempt (should be throttled)")
    asyncio.run(_run())
    budget_emails = [c for c in captured if "budget exceeded" in c["subject"].lower()]
    assert len(budget_emails) == 1  # the second send is throttled
    _reset(client, admin_token)
