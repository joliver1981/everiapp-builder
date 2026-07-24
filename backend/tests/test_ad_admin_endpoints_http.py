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
