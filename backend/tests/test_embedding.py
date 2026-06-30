"""Iframe embedding: per-app config, signed embed tokens, framed bootstrap."""
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
_DB = _TMP / "test_embedding.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_embedding")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "embedding-test")

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


def _insert_app(app_id: str, name: str = "Embed App"):
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
            (app_id, name, row[0]),
        )
        conn.commit()
    finally:
        conn.close()


def test_embed_config_roundtrip(client, admin_token):
    app_id = str(uuid.uuid4())
    _insert_app(app_id)

    # Disabled by default
    r = client.get(f"/api/apps/{app_id}/embed-config", headers=_auth(admin_token))
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    assert r.json()["snippet"] == ""

    # Enable with an allow-list
    r = client.put(f"/api/apps/{app_id}/embed-config",
                   json={"enabled": True, "allowed_origins": ["https://portal.acme.com"]},
                   headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is True
    assert body["allowed_origins"] == ["https://portal.acme.com"]
    assert "<iframe" in body["snippet"]
    assert f"/api/apps/{app_id}/embed" in body["embed_url"]


def test_invalid_origin_rejected(client, admin_token):
    app_id = str(uuid.uuid4())
    _insert_app(app_id)
    r = client.put(f"/api/apps/{app_id}/embed-config",
                   json={"enabled": True, "allowed_origins": ["not-a-url"]},
                   headers=_auth(admin_token))
    assert r.status_code == 400


def test_embed_token(client, admin_token):
    from src.embedding.service import verify_embed_token
    app_id = str(uuid.uuid4())
    _insert_app(app_id)

    # Token mint requires embedding to be on
    r = client.post(f"/api/apps/{app_id}/embed-token", headers=_auth(admin_token))
    assert r.status_code == 409

    client.put(f"/api/apps/{app_id}/embed-config",
               json={"enabled": True, "allowed_origins": []}, headers=_auth(admin_token))
    r = client.post(f"/api/apps/{app_id}/embed-token", headers=_auth(admin_token))
    assert r.status_code == 200
    token = r.json()["token"]
    assert r.json()["expires_in"] > 0
    assert verify_embed_token(token) == app_id
    assert verify_embed_token("garbage.token.here") is None


def test_embed_bootstrap_sets_frame_ancestors(client, admin_token):
    app_id = str(uuid.uuid4())
    _insert_app(app_id, name="Framed")

    # Not enabled → public bootstrap is 404
    assert client.get(f"/api/apps/{app_id}/embed").status_code == 404

    client.put(f"/api/apps/{app_id}/embed-config",
               json={"enabled": True, "allowed_origins": ["https://portal.acme.com"]},
               headers=_auth(admin_token))

    # Enabled → public, HTML, with a frame-ancestors CSP naming the allowed origin
    r = client.get(f"/api/apps/{app_id}/embed")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    csp = r.headers.get("content-security-policy", "")
    assert "frame-ancestors" in csp
    assert "https://portal.acme.com" in csp
    assert "'self'" in csp
    assert f"/apps/{app_id}/view" in r.text


def test_embed_wildcard_when_no_origins(client, admin_token):
    app_id = str(uuid.uuid4())
    _insert_app(app_id)
    client.put(f"/api/apps/{app_id}/embed-config",
               json={"enabled": True, "allowed_origins": []}, headers=_auth(admin_token))
    r = client.get(f"/api/apps/{app_id}/embed")
    assert r.status_code == 200
    assert "frame-ancestors *" in r.headers.get("content-security-policy", "")
