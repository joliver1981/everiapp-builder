"""Admin AD test/search endpoints must use the CONFIGURED LDAP provider.

Field bug (v0.17.4): a production install configured LDAP under Admin →
Platform → Authentication, but the Users & Roles page's "Test connection"
answered "Mock mode — no real AD connection needed" and the directory search
only knew the three dev mock users. The endpoints called the legacy env-based
ad_client (AD_MODE, default "mock") and never looked at the DB-configured
identity provider. Resolution ladder now under test:

    1. an ENABLED LDAP identity provider row  → test/search that provider
    2. no provider, settings.debug            → legacy dev mock (unchanged)
    3. no provider, production                → honest "not configured" guidance

Real LDAP is faked at the provider seam (LdapAuthProvider.test_connection /
search_users) — routing is what's under test, not ldap3.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_ad_admin_endpoints.db"
if _DB.exists():
    _DB.unlink()
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_DB}")
os.environ.setdefault("APP_DATA_DIR", str(_TMP / "apps_ad_admin"))
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "ad-admin-endpoints-test")

from src.main import app  # noqa: E402
from src.config import settings  # noqa: E402
from src.auth.providers.ldap_provider import LdapAuthProvider, LdapSearchError  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin_headers(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "password"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def ldap_provider_row(client, admin_headers):
    """An ENABLED LDAP provider created through the real admin API (no
    second event loop touching the shared async engine), deleted after."""
    r = client.post("/api/admin/auth-providers", headers=admin_headers, json={
        "provider_type": "ldap",
        "provider_name": "Corp AD",
        "is_enabled": True,
        "config": {
            "server": "dc01.corp.local",
            "base_dn": "DC=corp,DC=local",
            "service_bind_dn": "CN=svc,DC=corp,DC=local",
            "bind_password": "pw",
        },
    })
    assert r.status_code in (200, 201), r.text
    provider_id = r.json()["id"]
    yield provider_id
    client.delete(f"/api/admin/auth-providers/{provider_id}", headers=admin_headers)


def test_with_enabled_provider_test_uses_it_never_mock(client, admin_headers, ldap_provider_row, monkeypatch):
    monkeypatch.setattr(
        LdapAuthProvider, "test_connection",
        lambda self: (True, "Connected to dc01.corp.local:389 and bound as the service account"),
    )
    r = client.post("/api/admin/ad/test", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["mode"] == "ldap"
    assert "Corp AD" in body["message"]
    assert "Mock mode" not in body["message"]


def test_with_enabled_provider_search_returns_directory_rows(client, admin_headers, ldap_provider_row, monkeypatch):
    monkeypatch.setattr(
        LdapAuthProvider, "search_users",
        lambda self, q, limit=20: [
            {"username": "jdoe", "display_name": "Jane Doe", "email": "jdoe@corp.local"},
        ],
    )
    r = client.get("/api/admin/ad/search", params={"q": "doe"}, headers=admin_headers)
    assert r.status_code == 200
    assert r.json() == [
        {"username": "jdoe", "display_name": "Jane Doe", "email": "jdoe@corp.local"},
    ]


def test_provider_search_error_surfaces_as_400_detail(client, admin_headers, ldap_provider_row, monkeypatch):
    def _boom(self, q, limit=20):
        raise LdapSearchError("Service account bind failed — check the service account DN and password on the LDAP provider")
    monkeypatch.setattr(LdapAuthProvider, "search_users", _boom)
    r = client.get("/api/admin/ad/search", params={"q": "doe"}, headers=admin_headers)
    assert r.status_code == 400
    assert "Service account bind failed" in r.json()["detail"]


def test_production_without_provider_is_honest_not_mock(client, admin_headers, monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    r = client.post("/api/admin/ad/test", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is False
    assert body["mode"] == "unconfigured"
    assert "Mock mode" not in body["message"]
    assert "Authentication" in body["message"]  # points at the config page

    r2 = client.get("/api/admin/ad/search", params={"q": "doe"}, headers=admin_headers)
    assert r2.status_code == 400
    assert "No LDAP identity provider" in r2.json()["detail"]


def test_debug_without_provider_keeps_dev_mock(client, admin_headers):
    # settings.debug is True in the test env; no provider row exists here.
    r = client.post("/api/admin/ad/test", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["mode"] == "mock"

    r2 = client.get("/api/admin/ad/search", params={"q": "adm"}, headers=admin_headers)
    assert r2.status_code == 200
    assert any(u["username"] == "admin" for u in r2.json())


def test_provision_ad_user_creates_ldap_account(client, admin_headers):
    r = client.post("/api/admin/ad/provision", headers=admin_headers, json={
        "username": "jdoe", "display_name": "Jane Doe",
        "email": "jdoe@corp.local", "role": "developer",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["username"] == "jdoe"
    assert body["role"] == "developer"
    assert body["is_active"] is True
    # Visible in the users list, provisioned as an LDAP identity.
    listing = client.get("/api/admin/users", headers=admin_headers)
    assert listing.status_code == 200
    row = next(u for u in listing.json() if u["username"] == "jdoe")
    assert row["role"] == "developer"

    # Duplicate → 409, invalid role → 400.
    dup = client.post("/api/admin/ad/provision", headers=admin_headers,
                      json={"username": "jdoe", "role": "user"})
    assert dup.status_code == 409
    bad = client.post("/api/admin/ad/provision", headers=admin_headers,
                      json={"username": "zz-new", "role": "superadmin"})
    assert bad.status_code == 400


def test_provision_requires_admin(client):
    r = client.post("/api/auth/login", json={"username": "user", "password": "password"})
    assert r.status_code == 200
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    resp = client.post("/api/admin/ad/provision", headers=headers,
                       json={"username": "nope", "role": "admin"})
    assert resp.status_code in (401, 403)


def test_provisioned_role_sticks_without_group_mapping(aiosqlite_drain):
    """The pre-provision contract: without a group→role mapping the assigned
    role survives logins; with one, AD groups win on every login."""
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from src.database import Base
    from src.auth.models import User
    from src.auth.providers.base import AuthResult
    from src.auth.providers.chain import provision_user

    async def _run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            from src.auth.models import RefreshToken  # noqa: F401
            from src.apps.models import App, AppPermission, AppSetting, AppVersion, Conversation, Message  # noqa: F401
            from src.secrets.models import AuditLog, Secret  # noqa: F401
            from src.marketplace.models import MarketplaceListing  # noqa: F401
            from src.deployments.models import Deployment, DeploymentTarget  # noqa: F401
            from src.bug_reports.models import BugAnalysis, BugReport, FixAttempt  # noqa: F401
            from src.connections.models import Connection  # noqa: F401
            from src.datasets.models import AppDatasetBinding, Dataset  # noqa: F401
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        result = AuthResult(success=True, username="pat", display_name="Pat",
                            email="pat@corp.local", external_id="pat", groups=[])
        async with Session() as s:
            # Pre-provisioned admin (as POST /api/admin/ad/provision creates).
            s.add(User(username="pat", display_name="Pat", email="pat@corp.local",
                       role="admin", auth_provider="ldap", external_id="pat"))
            await s.commit()

            # Login WITHOUT mapping: role must stick.
            u = await provision_user(s, auth_provider="ldap", result=result,
                                     role="user", auto_provision=True,
                                     role_from_mapping=False)
            assert u is not None and u.role == "admin"

            # Login WITH mapping: AD groups are the source of truth.
            u = await provision_user(s, auth_provider="ldap", result=result,
                                     role="developer", auto_provision=True,
                                     role_from_mapping=True)
            assert u is not None and u.role == "developer"
            await s.commit()
        await engine.dispose()

    asyncio.run(_run())


def test_default_search_filter_excludes_computer_accounts():
    provider = LdapAuthProvider.__new__(LdapAuthProvider)
    provider._config = {}
    f = provider._build_search_filter("jol")
    assert "(!(objectClass=computer))" in f


def test_search_filter_is_escaped_against_injection():
    provider = LdapAuthProvider.__new__(LdapAuthProvider)  # skip ldap3 import guard
    provider._config = {}
    f = provider._build_search_filter("do*e)(objectClass=*")
    # RFC 4515 escaping: *, (, ) must be hex-escaped, so the crafted query
    # cannot break out of the filter expression.
    assert "do\\2ae" in f and "\\28" in f and "\\29" in f
    assert "(objectClass=*)" not in f.replace("(objectClass=user)", "")


def test_custom_query_filter_template_is_used():
    provider = LdapAuthProvider.__new__(LdapAuthProvider)
    provider._config = {"user_query_filter": "(uid=*{query}*)"}
    assert provider._build_search_filter("jo") == "(uid=*jo*)"
