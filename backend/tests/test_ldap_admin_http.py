"""TestClient tests for the auth-provider admin endpoints + the full
LDAP-then-local fallback in the auth service."""
from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_ldap_admin.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_ldap")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "ldap-admin-test")

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
def admin_token(client: TestClient) -> str:
    r = client.post("/api/auth/login", json={"username": "admin", "password": "password"})
    return r.json()["access_token"]


def _auth(t): return {"Authorization": f"Bearer {t}"}


def test_create_list_ldap_provider(client, admin_token):
    body = {
        "provider_type": "ldap",
        "provider_name": "Corp AD",
        "config": {
            "server": "dc01.corp.local",
            "port": 389,
            "base_dn": "DC=corp,DC=local",
            "bind_template": "{username}@corp.local",
            "bind_password": "super-secret",
        },
        "group_role_mapping": {"Domain Admins": "admin", "Developers": "developer"},
        "default_role": "user",
        "auto_provision": True,
        "is_enabled": True,
        "is_default": True,
    }
    r = client.post("/api/admin/auth-providers", json=body, headers=_auth(admin_token))
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["provider_name"] == "Corp AD"
    # Secret must be scrubbed in the response
    assert created["config"]["bind_password"] == "***REDACTED***"
    assert "super-secret" not in r.text

    # List
    r = client.get("/api/admin/auth-providers", headers=_auth(admin_token))
    assert r.status_code == 200
    assert any(p["provider_name"] == "Corp AD" for p in r.json())


def test_update_preserves_secret_when_redacted(client, admin_token):
    # Create
    r = client.post("/api/admin/auth-providers", json={
        "provider_type": "ldap", "provider_name": "Edit Me",
        "config": {"server": "x", "bind_password": "keepme"},
    }, headers=_auth(admin_token))
    pid = r.json()["id"]

    # Update with the redacted placeholder — secret must be preserved
    r = client.put(f"/api/admin/auth-providers/{pid}", json={
        "config": {"server": "y", "bind_password": "***REDACTED***"},
    }, headers=_auth(admin_token))
    assert r.status_code == 200

    # Verify via the auth service that the real password survived: re-read raw
    async def _check():
        from src.database import async_session
        from src.auth.models import IdentityProviderConfig
        from sqlalchemy import select
        import json
        async with async_session() as db:
            p = (await db.execute(select(IdentityProviderConfig).where(
                IdentityProviderConfig.id == pid))).scalar_one()
            cfg = json.loads(p.config_json)
            return cfg
    cfg = asyncio.run(_check())
    assert cfg["server"] == "y"  # updated
    assert cfg["bind_password"] == "keepme"  # preserved


def test_delete_provider(client, admin_token):
    r = client.post("/api/admin/auth-providers", json={
        "provider_type": "ldap", "provider_name": "Delete Me", "config": {},
    }, headers=_auth(admin_token))
    pid = r.json()["id"]
    r = client.delete(f"/api/admin/auth-providers/{pid}", headers=_auth(admin_token))
    assert r.status_code == 204


def test_non_admin_cannot_manage_providers(client):
    r = client.post("/api/auth/login", json={"username": "developer", "password": "password"})
    dev = r.json()["access_token"]
    r = client.get("/api/admin/auth-providers", headers=_auth(dev))
    assert r.status_code in (401, 403)


def test_ldap_login_then_local_fallback(client, admin_token):
    """End-to-end: an enabled LDAP provider that authenticates a directory user,
    plus the local-admin fallback still working when LDAP rejects."""
    # Isolation: clear any providers left by other test files sharing this
    # process's engine, so the chain has exactly the one we're about to create.
    for p in client.get("/api/admin/auth-providers", headers=_auth(admin_token)).json():
        client.delete(f"/api/admin/auth-providers/{p['id']}", headers=_auth(admin_token))

    # Enable a default LDAP provider
    client.post("/api/admin/auth-providers", json={
        "provider_type": "ldap", "provider_name": "Login AD",
        "config": {"server": "dc.corp.local", "bind_template": "{username}@corp.local"},
        "group_role_mapping": {"Developers": "developer"},
        "default_role": "user", "auto_provision": True, "is_enabled": True, "is_default": True,
    }, headers=_auth(admin_token))

    # Mock a successful LDAP bind for "ldapuser"
    entry = MagicMock()
    entry.displayName.value = "LDAP User"
    entry.mail.value = "ldapuser@corp.local"
    entry.sAMAccountName.value = "ldapuser"
    entry.memberOf.values = ["CN=Developers,DC=corp,DC=local"]
    fake_conn = MagicMock()
    fake_conn.entries = [entry]

    with patch("src.auth.providers.ldap_provider.Connection", return_value=fake_conn):
        r = client.post("/api/auth/login", json={"username": "ldapuser", "password": "pw"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["username"] == "ldapuser"
    assert body["user"]["role"] == "developer"  # mapped from Developers group

    # Local admin still works (LDAP rejects unknown → falls back to mock)
    from ldap3.core.exceptions import LDAPBindError
    with patch("src.auth.providers.ldap_provider.Connection", side_effect=LDAPBindError("nope")):
        r = client.post("/api/auth/login", json={"username": "admin", "password": "password"})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["username"] == "admin"
