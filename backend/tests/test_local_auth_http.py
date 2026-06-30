"""First-run local auth via the real HTTP routes.

Covers the new standalone-install flow: a fresh install has NO working
credentials until an admin is created via POST /api/auth/bootstrap-admin; the
old hardcoded mock creds (admin/password) are DEV-ONLY and must NOT work when
DEBUG is off (the security fix).
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_local_auth.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_local_auth")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "local-auth-test")

from src.config import settings  # noqa: E402
from src.database import init_db  # noqa: E402
from src.main import app  # noqa: E402

ADMIN_USER = "rootadmin"
ADMIN_PASS = "adminpass123"


async def _reset_db():
    # This module asserts a FRESH-INSTALL state (needs_admin=True, first bootstrap).
    # In the full suite the global engine binds to whichever test module imported
    # `src` first (settings is a singleton), so a prior module's admin can leak in
    # and make the bootstrap tests fail with 409. Reset the bound DB to empty here
    # so these tests are deterministic regardless of collection order.
    from src.database import Base, engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()


@pytest.fixture(scope="module", autouse=True)
def _init():
    asyncio.run(_reset_db())
    yield


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


# ---- fresh install ----------------------------------------------------------

def test_fresh_install_needs_admin(client):
    r = client.get("/api/setup/status")
    assert r.status_code == 200
    body = r.json()
    assert body["needs_admin"] is True


def test_bootstrap_validates_input(client):
    # too-short password / bad username are rejected (422 from pydantic)
    assert client.post("/api/auth/bootstrap-admin",
                       json={"username": "ok", "password": "short"}).status_code == 422
    assert client.post("/api/auth/bootstrap-admin",
                       json={"username": "a b", "password": "longenough1"}).status_code == 422


def test_bootstrap_creates_first_admin(client):
    r = client.post("/api/auth/bootstrap-admin",
                    json={"username": ADMIN_USER, "password": ADMIN_PASS})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["access_token"]
    assert body["user"]["username"] == ADMIN_USER
    assert body["user"]["role"] == "admin"
    # The access token works against an authed endpoint.
    me = client.get("/api/auth/me", headers=_auth(body["access_token"]))
    assert me.status_code == 200 and me.json()["user"]["role"] == "admin"
    # No longer a fresh install.
    assert client.get("/api/setup/status").json()["needs_admin"] is False


def test_bootstrap_refused_once_admin_exists(client):
    r = client.post("/api/auth/bootstrap-admin",
                    json={"username": "second", "password": "anotherpass1"})
    assert r.status_code == 409
    assert "already exists" in r.json()["detail"]


def test_local_login_works(client):
    r = client.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["username"] == ADMIN_USER


def test_local_login_wrong_password(client):
    r = client.post("/api/auth/login", json={"username": ADMIN_USER, "password": "nope"})
    assert r.status_code == 401


# ---- the security fix: mock creds are DEV-only ------------------------------

def test_mock_creds_disabled_when_debug_off(client, monkeypatch):
    # 'developer'/'password' is a mock-only account (no local user).
    monkeypatch.setattr(settings, "debug", False)
    r = client.post("/api/auth/login", json={"username": "developer", "password": "password"})
    assert r.status_code == 401  # production: hardcoded mock creds do NOT work
    # The real local admin still works regardless of DEBUG.
    assert client.post("/api/auth/login",
                       json={"username": ADMIN_USER, "password": ADMIN_PASS}).status_code == 200


def test_mock_creds_work_in_dev(client, monkeypatch):
    monkeypatch.setattr(settings, "debug", True)
    r = client.post("/api/auth/login", json={"username": "developer", "password": "password"})
    assert r.status_code == 200  # dev convenience still available


# ---- admin user management + self change-password ---------------------------

def _admin_token(client):
    return client.post("/api/auth/login",
                       json={"username": ADMIN_USER, "password": ADMIN_PASS}).json()["access_token"]


def test_admin_create_user_and_login(client):
    tok = _admin_token(client)
    r = client.post("/api/admin/users",
                    json={"username": "devuser", "password": "devpass123", "role": "developer"},
                    headers=_auth(tok))
    assert r.status_code == 201, r.text
    assert r.json()["username"] == "devuser" and r.json()["role"] == "developer"
    # The new local user can log in.
    assert client.post("/api/auth/login",
                       json={"username": "devuser", "password": "devpass123"}).status_code == 200
    # Duplicate username rejected.
    assert client.post("/api/admin/users",
                       json={"username": "devuser", "password": "whatever1"},
                       headers=_auth(tok)).status_code == 409


def test_change_own_password(client):
    tok = client.post("/api/auth/login",
                      json={"username": "devuser", "password": "devpass123"}).json()["access_token"]
    # wrong current password rejected
    assert client.post("/api/auth/change-password",
                       json={"current_password": "wrong", "new_password": "newdevpass1"},
                       headers=_auth(tok)).status_code == 400
    r = client.post("/api/auth/change-password",
                    json={"current_password": "devpass123", "new_password": "newdevpass1"},
                    headers=_auth(tok))
    assert r.status_code == 200, r.text
    # old fails, new works
    assert client.post("/api/auth/login",
                       json={"username": "devuser", "password": "devpass123"}).status_code == 401
    assert client.post("/api/auth/login",
                       json={"username": "devuser", "password": "newdevpass1"}).status_code == 200


def test_admin_reset_password(client):
    tok = _admin_token(client)
    dev = client.get("/api/admin/users", headers=_auth(tok)).json()
    devuser = next(u for u in dev if u["username"] == "devuser")
    r = client.post(f"/api/admin/users/{devuser['id']}/reset-password",
                    json={"new_password": "resetpass123"}, headers=_auth(tok))
    assert r.status_code == 200, r.text
    assert client.post("/api/auth/login",
                       json={"username": "devuser", "password": "resetpass123"}).status_code == 200
