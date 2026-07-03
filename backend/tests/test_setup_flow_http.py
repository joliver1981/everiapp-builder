"""Setup-wizard end-to-end flow via the real HTTP routes.

Covers the Wave-1/2 wizard chain: server-side schema validation, the
post-install setup endpoints (status + apply, with encryption), the relaxed
resolved-settings access rule the SDK depends on, connection/global_secret
field types, and the minimal pickable lists for non-admin users.
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
_DB = _TMP / "test_setup_flow.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_setup_flow")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "setup-flow-test")

from src.database import init_db  # noqa: E402
from src.main import app  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _init():
    asyncio.run(init_db())


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _login(client, username, password="password"):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def admin(client):
    return _login(client, "admin")


@pytest.fixture(scope="module")
def developer(client):
    return _login(client, "developer")


@pytest.fixture(scope="module")
def plain_user(client):
    return _login(client, "user")


WIZARD = {
    "title": "ERP Setup",
    "description": "Connect the app to your company systems",
    "steps": [
        {
            "title": "Credentials",
            "fields": [
                {"key": "api_token", "label": "API token", "type": "secret", "required": True},
                {"key": "region", "label": "Region", "type": "select",
                 "options": ["us", "eu"], "required": True},
                {"key": "welcome_text", "label": "Welcome text", "type": "string",
                 "default": "Hello"},
            ],
        },
    ],
}


@pytest.fixture(scope="module")
def wizard_app(client, admin):
    r = client.post("/api/apps", json={"name": "Setup Flow App"}, headers=admin)
    assert r.status_code == 201, r.text
    app_id = r.json()["id"]
    r = client.put(f"/api/apps/{app_id}/wizard", json=WIZARD, headers=admin)
    assert r.status_code == 200, r.text
    return app_id


# ---------------------------------------------------------------- validation

def test_wizard_validation_rejects_bad_schemas(client, admin, wizard_app):
    cases = [
        ({"steps": [{"fields": [{"key": "a"}, {"key": "a"}]}]}, "duplicate"),
        ({"steps": [{"fields": [{"key": "bad key!"}]}]}, "identifier"),
        ({"steps": [{"fields": [{"key": "a", "type": "nope"}]}]}, "unknown type"),
        ({"steps": [{"fields": [{"key": "a", "type": "select"}]}]}, "options"),
        ({"steps": [{"fields": [{"label": "no key"}]}]}, "required"),
    ]
    for schema, needle in cases:
        r = client.put(f"/api/apps/{wizard_app}/wizard", json=schema, headers=admin)
        assert r.status_code == 400, f"{schema} -> {r.status_code}: {r.text}"
        assert needle in r.json()["detail"], (needle, r.json()["detail"])


def test_wizard_keeps_description_and_new_types(client, admin, wizard_app):
    schema = {
        "title": "T",
        "description": "This description must round-trip now",
        "steps": [{
            "title": "S",
            "fields": [
                {"key": "db", "label": "ERP database", "type": "connection", "dialect": "sqlite"},
                {"key": "shared_key", "label": "Shared key", "type": "global_secret"},
            ],
        }],
    }
    r = client.put(f"/api/apps/{wizard_app}/wizard", json=schema, headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["description"] == "This description must round-trip now"
    # restore the module wizard for the following tests
    r = client.put(f"/api/apps/{wizard_app}/wizard", json=WIZARD, headers=admin)
    assert r.status_code == 200, r.text


# ------------------------------------------------------- setup status + apply

def test_setup_status_reports_missing_required(client, admin, wizard_app):
    r = client.get(f"/api/apps/{wizard_app}/setup-status", headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_wizard"] is True
    assert body["complete"] is False
    assert body["required_total"] == 2
    assert {m["key"] for m in body["missing"]} == {"api_token", "region"}


def test_apply_setup_encrypts_and_completes(client, admin, wizard_app):
    r = client.post(f"/api/apps/{wizard_app}/setup", json={
        "values": {"api_token": "tok-abc", "region": "eu", "welcome_text": "Hi"},
    }, headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] == 3
    assert body["complete"] is True and body["missing"] == []

    # Secret stored encrypted (masked in the list API, ciphertext in DB).
    r = client.get(f"/api/apps/{wizard_app}/settings", headers=admin)
    rows = {s["key"]: s for s in r.json()}
    assert rows["api_token"]["value"] != "tok-abc"

    # Resolved settings decrypt the secret back.
    r = client.get(f"/api/apps/{wizard_app}/settings/resolved", headers=admin)
    assert r.status_code == 200, r.text
    resolved = r.json()
    assert resolved["api_token"] == "tok-abc"
    assert resolved["region"] == "eu"


def test_apply_setup_upserts_on_rerun(client, admin, wizard_app):
    """Re-running setup must update rows, not duplicate them."""
    r = client.post(f"/api/apps/{wizard_app}/setup", json={
        "values": {"api_token": "tok-v2"},
    }, headers=admin)
    assert r.status_code == 200, r.text
    r = client.get(f"/api/apps/{wizard_app}/settings", headers=admin)
    keys = [s["key"] for s in r.json()]
    assert keys.count("api_token") == 1
    r = client.get(f"/api/apps/{wizard_app}/settings/resolved", headers=admin)
    assert r.json()["api_token"] == "tok-v2"


def test_setup_requires_wizard(client, admin):
    r = client.post("/api/apps", json={"name": "No Wizard App"}, headers=admin)
    app_id = r.json()["id"]
    r = client.post(f"/api/apps/{app_id}/setup", json={"values": {"x": "1"}}, headers=admin)
    assert r.status_code == 400
    r = client.get(f"/api/apps/{app_id}/setup-status", headers=admin)
    assert r.json() == {"has_wizard": False, "complete": True, "missing": [], "required_total": 0}


# --------------------------------------- garbage wizards stored pre-validation

def _force_stored_wizard(app_id: str, wizard) -> None:
    """Write setup_wizard directly, bypassing the (now validated) write paths —
    simulates rows created before validation existed."""
    import json as _json
    import sqlite3
    from src.config import settings
    # settings wins over this module's _DB when the whole suite shares a process.
    conn = sqlite3.connect(settings.database_url[len("sqlite+aiosqlite:///"):])
    try:
        conn.execute("UPDATE apps SET setup_wizard = ? WHERE id = ?",
                     (_json.dumps(wizard), app_id))
        conn.commit()
    finally:
        conn.close()


def test_garbage_wizard_rows_degrade_instead_of_500(client, admin):
    """Steps-of-strings rows used to AttributeError → 500 on setup-status/setup."""
    r = client.post("/api/apps", json={"name": "Garbage Wizard App"}, headers=admin)
    app_id = r.json()["id"]
    _force_stored_wizard(app_id, {"steps": ["step one", "step two"]})

    r = client.get(f"/api/apps/{app_id}/setup-status", headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["complete"] is True          # nothing enforceable in garbage

    r = client.post(f"/api/apps/{app_id}/setup", json={"values": {"x": "y"}}, headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["applied"] == 0


def test_non_dict_wizard_row_behaves_like_no_wizard(client, admin):
    r = client.post("/api/apps", json={"name": "String Wizard App"}, headers=admin)
    app_id = r.json()["id"]
    _force_stored_wizard(app_id, "hello")

    r = client.get(f"/api/apps/{app_id}/setup-status", headers=admin)
    assert r.status_code == 200 and r.json()["has_wizard"] is False
    assert client.post(f"/api/apps/{app_id}/setup",
                       json={"values": {"x": "1"}}, headers=admin).status_code == 400
    assert client.get(f"/api/apps/{app_id}/wizard", headers=admin).json() == {}


def test_sanitized_wizard_gate():
    """Lenient gate used by marketplace installs for pre-validation listings."""
    from src.apps.service import sanitized_wizard
    good = {"steps": [{"fields": [{"key": "a"}]}]}
    assert sanitized_wizard(good) == good
    assert sanitized_wizard(None) is None
    assert sanitized_wizard("hello") is None
    assert sanitized_wizard({"steps": ["a"]}) is None
    assert sanitized_wizard({"steps": [{"fields": [{"key": "dup"}, {"key": "dup"}]}]}) is None


# ------------------------------------------- resolved-settings access (SDK fix)

def test_plain_user_can_resolve_open_app_settings(client, plain_user, wizard_app):
    """The SDK fetches resolved settings as the VIEWING user — regular users
    with app access must get 200 (this was admin/developer-only, breaking
    useAppConfig for everyone else)."""
    r = client.get(f"/api/apps/{wizard_app}/settings/resolved", headers=plain_user)
    assert r.status_code == 200, r.text
    assert r.json()["api_token"] == "tok-v2"
    r = client.get(f"/api/apps/{wizard_app}/setup-status", headers=plain_user)
    assert r.status_code == 200 and r.json()["complete"] is True


def test_plain_user_blocked_on_restricted_app(client, admin, plain_user):
    r = client.post("/api/apps", json={"name": "Restricted App"}, headers=admin)
    app_id = r.json()["id"]
    # Restrict access to a group the mock user isn't in.
    r = client.post(f"/api/apps/{app_id}/permissions", json={
        "group_name": "definitely-not-a-real-group",
    }, headers=admin)
    assert r.status_code in (200, 201), r.text
    r = client.get(f"/api/apps/{app_id}/settings/resolved", headers=plain_user)
    assert r.status_code == 403
    r = client.get(f"/api/apps/{app_id}/setup-status", headers=plain_user)
    assert r.status_code == 403


def test_resolved_settings_require_auth(client, wizard_app):
    r = client.get(f"/api/apps/{wizard_app}/settings/resolved")
    assert r.status_code in (401, 403)


# ------------------------------------- connection + global_secret field types

@pytest.fixture(scope="module")
def sqlite_connection(client, admin):
    db_path = _TMP / "setup_flow_conn.db"
    r = client.post("/api/admin/connections", json={
        "name": "Test SQLite",
        "kind": "sql",
        "config": {"dialect": "sqlite", "database": str(db_path)},
    }, headers=admin)
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture(scope="module")
def global_secret(client, admin):
    r = client.post("/api/secrets", json={
        "name": "wizard-shared-secret", "category": "custom", "value": "glob-val-9",
    }, headers=admin)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_pickable_lists_for_developers(client, developer, plain_user, sqlite_connection, global_secret):
    # Developers see minimal fields only — no config/credentials.
    r = client.get("/api/admin/connections/pickable", headers=developer)
    assert r.status_code == 200, r.text
    conns = r.json()
    mine = next(c for c in conns if c["id"] == sqlite_connection)
    assert mine["dialect"] == "sqlite"
    assert set(mine.keys()) == {"id", "name", "description", "kind", "dialect"}

    r = client.get("/api/secrets/pickable", headers=developer)
    assert r.status_code == 200, r.text
    secs = r.json()
    mine = next(s for s in secs if s["id"] == global_secret)
    assert mine["is_set"] is True
    assert set(mine.keys()) == {"id", "name", "category", "is_set"}

    # Regular users get neither.
    assert client.get("/api/admin/connections/pickable", headers=plain_user).status_code == 403
    assert client.get("/api/secrets/pickable", headers=plain_user).status_code == 403


def test_developer_can_test_connection(client, developer, sqlite_connection):
    r = client.post(f"/api/admin/connections/{sqlite_connection}/test", headers=developer)
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True, r.json()


def test_connection_and_global_secret_fields_resolve(client, admin, sqlite_connection, global_secret):
    """A wizard with connection + global_secret fields: apply stores the
    connection id as a value and the secret as a POINTER (global_secret_ref),
    and resolved settings decrypt the global secret's value."""
    r = client.post("/api/apps", json={"name": "ERP Bound App"}, headers=admin)
    app_id = r.json()["id"]
    r = client.put(f"/api/apps/{app_id}/wizard", json={
        "title": "Bind",
        "steps": [{
            "title": "S1",
            "fields": [
                {"key": "erp_db", "label": "ERP database", "type": "connection",
                 "dialect": "sqlite", "required": True},
                {"key": "shared_api_key", "label": "Shared API key",
                 "type": "global_secret", "required": True},
            ],
        }],
    }, headers=admin)
    assert r.status_code == 200, r.text

    r = client.get(f"/api/apps/{app_id}/setup-status", headers=admin)
    assert r.json()["complete"] is False and r.json()["required_total"] == 2

    r = client.post(f"/api/apps/{app_id}/setup", json={
        "values": {"erp_db": sqlite_connection, "shared_api_key": global_secret},
    }, headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["complete"] is True

    r = client.get(f"/api/apps/{app_id}/settings/resolved", headers=admin)
    resolved = r.json()
    assert resolved["erp_db"] == sqlite_connection          # the app reads the id
    assert resolved["shared_api_key"] == "glob-val-9"       # pointer -> decrypted value

    # The pointer is a ref, not a copied value: rotating the global secret
    # changes what the app resolves without touching the app.
    r = client.put(f"/api/secrets/{global_secret}", json={"value": "glob-val-10"}, headers=admin)
    assert r.status_code == 200, r.text
    r = client.get(f"/api/apps/{app_id}/settings/resolved", headers=admin)
    assert r.json()["shared_api_key"] == "glob-val-10"


# --------------------------- privilege-escalation regression (platform secrets)

@pytest.fixture(scope="module")
def platform_secret(client, admin):
    """A platform credential (ai_provider category) — must NEVER be app-bindable."""
    r = client.post("/api/secrets", json={
        "name": "llm-master-key", "category": "ai_provider", "value": "sk-ant-SUPERSECRET",
    }, headers=admin)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_platform_secrets_hidden_from_pickable(client, developer, platform_secret, global_secret):
    """Enumeration layer: /secrets/pickable only lists app-bindable categories."""
    r = client.get("/api/secrets/pickable", headers=developer)
    assert r.status_code == 200, r.text
    ids = {s["id"] for s in r.json()}
    assert global_secret in ids            # category=custom stays pickable
    assert platform_secret not in ids      # category=ai_provider hidden
    assert all(s["category"] in ("custom", "integration") for s in r.json())


def test_developer_cannot_bind_platform_secret(client, developer, platform_secret):
    """Write layer: binding a platform secret 400s on BOTH write paths.
    This was the confirmed escalation: developer binds any secret id into a
    self-owned app, then reads the decrypted value via /settings/resolved."""
    r = client.post("/api/apps", json={"name": "Escalation App"}, headers=developer)
    assert r.status_code == 201, r.text
    app_id = r.json()["id"]

    # Direct settings-create path
    r = client.post(f"/api/apps/{app_id}/settings", json={
        "key": "steal", "label": "steal", "type": "string",
        "global_secret_ref": platform_secret,
    }, headers=developer)
    assert r.status_code == 400, r.text
    assert "cannot be bound" in r.json()["detail"]

    # Settings-update path
    r = client.post(f"/api/apps/{app_id}/settings", json={
        "key": "steal2", "label": "steal2", "type": "string",
    }, headers=developer)
    assert r.status_code == 201, r.text
    setting_id = r.json()["id"]
    r = client.put(f"/api/apps/{app_id}/settings/{setting_id}", json={
        "global_secret_ref": platform_secret,
    }, headers=developer)
    assert r.status_code == 400, r.text

    # Wizard-apply path
    r = client.put(f"/api/apps/{app_id}/wizard", json={
        "steps": [{"fields": [{"key": "gk", "label": "g", "type": "global_secret"}]}],
    }, headers=developer)
    assert r.status_code == 200, r.text
    r = client.post(f"/api/apps/{app_id}/setup", json={
        "values": {"gk": platform_secret},
    }, headers=developer)
    assert r.status_code == 400, r.text

    # Nothing leaked through resolve.
    r = client.get(f"/api/apps/{app_id}/settings/resolved", headers=developer)
    assert r.status_code == 200
    assert "sk-ant-SUPERSECRET" not in r.text

    # A nonexistent ref is also a clean 400, not a silent dangling pointer.
    r = client.post(f"/api/apps/{app_id}/settings", json={
        "key": "dangling", "label": "d", "type": "string",
        "global_secret_ref": "no-such-secret-id",
    }, headers=developer)
    assert r.status_code == 400


def test_legacy_platform_ref_never_decrypts(client, admin, platform_secret):
    """Resolve layer (defense in depth): even a pre-existing row that points at
    a platform secret (bound before validation existed) must resolve to the
    default — never the decrypted credential.

    We simulate the legacy row by temporarily relaxing the bind policy for the
    write, then asserting resolve refuses it under the REAL policy. (Uses the
    app's own engine via the API — no second sqlite connection, which would
    lock against the async engine under the full suite.)"""
    from unittest.mock import patch

    r = client.post("/api/apps", json={"name": "Legacy Ref App"}, headers=admin)
    app_id = r.json()["id"]

    relaxed = frozenset({"custom", "integration", "ai_provider"})
    with patch("src.secrets.models.APP_BINDABLE_SECRET_CATEGORIES", relaxed):
        r = client.post(f"/api/apps/{app_id}/settings", json={
            "key": "legacy_ref", "label": "Legacy", "type": "string",
            "default_value": "fallback", "global_secret_ref": platform_secret,
        }, headers=admin)
        assert r.status_code == 201, r.text

    # Real policy restored: resolve must NOT decrypt a platform-category secret.
    r = client.get(f"/api/apps/{app_id}/settings/resolved", headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["legacy_ref"] == "fallback"
    assert "sk-ant-SUPERSECRET" not in r.text
