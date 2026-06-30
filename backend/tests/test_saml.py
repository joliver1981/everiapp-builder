"""SAML SSO: pure settings/identity/role logic (no crypto lib needed) + the
graceful behavior of the routes when python3-saml isn't installed.

The full assertion round-trip needs the [saml] extra + a real IdP and is
intentionally deferred — these lock in everything around it.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_saml.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_saml")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "saml-test")

from src.database import init_db  # noqa: E402
from src.main import app  # noqa: E402
from src.auth.saml import settings_builder as sb  # noqa: E402


# ---------------------------------------------------------------------------
# Pure logic — no python3-saml required
# ---------------------------------------------------------------------------
def test_build_saml_settings_derives_sp_urls():
    cfg = {"idp_entity_id": "https://idp/x", "idp_sso_url": "https://idp/sso",
           "idp_x509_cert": "CERT"}
    s = sb.build_saml_settings(cfg, "https://aihub.example.com/", "prov1")
    assert s["sp"]["entityId"] == "https://aihub.example.com/api/auth/saml/prov1/metadata"
    assert s["sp"]["assertionConsumerService"]["url"].endswith("/api/auth/saml/prov1/acs")
    assert s["idp"]["entityId"] == "https://idp/x"
    assert s["idp"]["x509cert"] == "CERT"
    assert s["security"]["wantAssertionsSigned"] is True


def test_build_saml_settings_honors_overrides():
    cfg = {"sp_entity_id": "custom-sp", "sp_acs_url": "https://x/acs",
           "idp_entity_id": "i", "idp_sso_url": "s", "idp_x509_cert": "c"}
    s = sb.build_saml_settings(cfg, "https://base/", "p")
    assert s["sp"]["entityId"] == "custom-sp"
    assert s["sp"]["assertionConsumerService"]["url"] == "https://x/acs"


def test_validate_saml_config():
    assert sb.validate_saml_config({"idp_entity_id": "a", "idp_sso_url": "b", "idp_x509_cert": "c"}) == []
    problems = sb.validate_saml_config({})
    assert len(problems) == 3


def test_extract_identity_with_mapping():
    attrs = {
        "urn:name": ["jdoe"],
        "urn:email": ["jdoe@corp.com"],
        "urn:dn": ["Jane Doe"],
        "urn:groups": ["Engineers", "Admins"],
    }
    mapping = {"username": "urn:name", "email": "urn:email",
               "display_name": "urn:dn", "groups": "urn:groups"}
    res = sb.extract_identity(attrs, "nameid-123", mapping)
    assert res.success is True
    assert res.username == "jdoe"
    assert res.email == "jdoe@corp.com"
    assert res.display_name == "Jane Doe"
    assert res.groups == ["Engineers", "Admins"]
    assert res.external_id == "nameid-123"


def test_extract_identity_falls_back_to_nameid():
    res = sb.extract_identity({}, "user@corp.com", {})
    assert res.username == "user@corp.com"
    assert res.display_name == "user@corp.com"


def test_resolve_role_highest_wins():
    mapping = {"Engineers": "developer", "Admins": "admin", "Staff": "user"}
    assert sb.resolve_role(["Engineers", "Admins"], mapping, "user") == "admin"
    assert sb.resolve_role(["Engineers"], mapping, "user") == "developer"
    assert sb.resolve_role(["Unknown"], mapping, "user") == "user"
    assert sb.resolve_role([], mapping, "user") == "user"
    # Tolerates AD distinguished names
    assert sb.resolve_role(["CN=Admins,OU=Groups,DC=corp"], mapping, "user") == "admin"


# ---------------------------------------------------------------------------
# Routes — graceful behavior without the [saml] extra installed
# ---------------------------------------------------------------------------
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


def _create_saml_provider(client, admin_token, *, config) -> str:
    r = client.post("/api/admin/auth-providers", json={
        "provider_type": "saml", "provider_name": f"SAML {uuid.uuid4().hex[:5]}",
        "config": config,
        "group_role_mapping": {"Admins": "admin"},
        "default_role": "user", "auto_provision": True, "is_enabled": True, "is_default": False,
    }, headers=_auth(admin_token))
    assert r.status_code == 201, r.text
    return r.json()


def test_saml_private_key_scrubbed(client, admin_token):
    created = _create_saml_provider(client, admin_token, config={
        "idp_entity_id": "https://idp/x", "idp_sso_url": "https://idp/sso",
        "idp_x509_cert": "PUBLICCERT", "sp_private_key": "TOP-SECRET-KEY",
    })
    assert created["config"]["sp_private_key"] == "***REDACTED***"
    assert "TOP-SECRET-KEY" not in str(created)


def test_public_provider_list(client, admin_token):
    _create_saml_provider(client, admin_token, config={
        "idp_entity_id": "https://idp/x", "idp_sso_url": "https://idp/sso", "idp_x509_cert": "C",
    })
    # Public — no auth header needed
    r = client.get("/api/auth/saml/providers")
    assert r.status_code == 200
    assert len(r.json()) >= 1
    assert all(set(p.keys()) == {"id", "name"} for p in r.json())


def test_login_invalid_config_is_400(client, admin_token):
    # Missing idp_sso_url + cert → config invalid, caught before any crypto
    prov = _create_saml_provider(client, admin_token, config={"idp_entity_id": "only-this"})
    r = client.get(f"/api/auth/saml/{prov['id']}/login", follow_redirects=False)
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "saml_config_invalid"


def test_routes_501_without_saml_extra(client, admin_token):
    """With python3-saml absent (this env), crypto routes surface a clean 501."""
    pytest.importorskip  # marker; we explicitly assert the not-installed path
    import importlib.util
    if importlib.util.find_spec("onelogin") is not None:
        pytest.skip("python3-saml IS installed; the 501 path doesn't apply")

    prov = _create_saml_provider(client, admin_token, config={
        "idp_entity_id": "https://idp/x", "idp_sso_url": "https://idp/sso", "idp_x509_cert": "C",
    })
    pid = prov["id"]
    assert client.get(f"/api/auth/saml/{pid}/metadata").status_code == 501
    assert client.get(f"/api/auth/saml/{pid}/login", follow_redirects=False).status_code == 501
    assert client.post(f"/api/auth/saml/{pid}/acs", data={"SAMLResponse": "x"}).status_code == 501


def test_unknown_provider_404(client):
    assert client.get("/api/auth/saml/does-not-exist/metadata").status_code == 404
