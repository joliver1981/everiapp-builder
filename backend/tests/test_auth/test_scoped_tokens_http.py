"""Scoped session tokens (purpose=preview/embed) — the v0.7.8 hardening.

The runtime proxy injects a 12h token into every previewed/viewed app as
window.__AIHUB_TOKEN__. That token is readable by the app's own (AI-generated)
JS, so it must be weaker than a login token:

  - require_role surfaces (admin/builder) REJECT it outright — an admin
    previewing an app must not hand that app admin API access;
  - it is app-scoped: usable only on routes of the app it was minted for;
  - SDK-facing endpoints (app-db, settings, auth/me) keep accepting it;
  - login tokens are unaffected.

Also covered: the embed transport (public bootstrap → __aihub_embed query →
scoped guest token injection), the username claim, and the chat WS typed
auth_error contract (scope rejection, expired tokens, deactivated accounts).
"""
import asyncio
import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_scoped_tokens.db"
if _DB.exists():
    try:
        _DB.unlink()
    except OSError:
        pass
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_scoped_tokens")
os.environ["DEBUG"] = "true"
os.environ.setdefault(
    "MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8="
)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

from src.auth.service import auth_service  # noqa: E402
from src.config import settings  # noqa: E402
from src.main import app  # noqa: E402
from src.runtime import proxy as proxy_mod  # noqa: E402
from src.runtime.manager import runtime_manager  # noqa: E402

settings.app_data_dir = os.environ["APP_DATA_DIR"]


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin_token(client: TestClient) -> str:
    r = client.post("/api/auth/login", json={"username": "admin", "password": "password"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(t: str) -> dict:
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture(scope="module")
def two_apps(client: TestClient, admin_token: str) -> tuple[str, str]:
    ids = []
    for i in range(2):
        r = client.post("/api/apps", json={"name": f"scoped-{i}-{uuid.uuid4().hex[:6]}"},
                        headers=_auth(admin_token))
        assert r.status_code in (200, 201), r.text
        ids.append(r.json()["id"])
    return ids[0], ids[1]


def _preview_token(admin_token: str, app_id: str) -> str:
    """Mint exactly what the runtime proxy injects for a previewing admin."""
    payload = auth_service.decode_access_token(admin_token)
    return auth_service.create_access_token(
        payload["sub"], payload["role"], expire_minutes=60,
        extra_claims={"purpose": "preview", "app_id": app_id,
                      "username": payload.get("username", "")},
    )


# ---- login tokens carry the username claim ---------------------------------

def test_login_token_carries_username_claim(admin_token):
    payload = auth_service.decode_access_token(admin_token)
    assert payload["username"] == "admin"


# ---- require_role rejection -------------------------------------------------

def test_preview_token_rejected_on_role_gated_route_even_for_its_own_app(
        client, admin_token, two_apps):
    app_a, _ = two_apps
    preview = _preview_token(admin_token, app_a)

    # Control: the same admin's LOGIN token reaches the role-gated route.
    r = client.get(f"/api/apps/{app_a}/embed-config", headers=_auth(admin_token))
    assert r.status_code == 200, r.text

    # The preview token carries the same admin identity — still rejected.
    r = client.get(f"/api/apps/{app_a}/embed-config", headers=_auth(preview))
    assert r.status_code == 403, r.text
    assert "session token" in r.json()["detail"].lower()


# ---- app scoping ------------------------------------------------------------

def test_preview_token_accepted_by_sdk_routes_of_its_app(client, admin_token, two_apps):
    app_a, _ = two_apps
    preview = _preview_token(admin_token, app_a)

    # app-db (the SDK's useAppDB path)
    r = client.post(f"/api/apps/{app_a}/db/migrate", headers=_auth(preview), json={
        "migrations": [{"version": 1, "name": "t",
                        "sql": "CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)"}]})
    assert r.status_code == 200, r.text
    r = client.post(f"/api/apps/{app_a}/db/query", headers=_auth(preview),
                    json={"sql": "SELECT COUNT(*) AS n FROM t", "params": {}, "scope": "all"})
    assert r.status_code == 200, r.text

    # /api/auth/me (the SDK's fetchUser)
    r = client.get("/api/auth/me", headers=_auth(preview))
    assert r.status_code == 200, r.text
    assert r.json()["user"]["username"] == "admin"


def test_preview_token_rejected_on_another_apps_routes(client, admin_token, two_apps):
    app_a, app_b = two_apps
    preview_a = _preview_token(admin_token, app_a)

    r = client.post(f"/api/apps/{app_b}/db/query", headers=_auth(preview_a),
                    json={"sql": "SELECT 1 AS x", "params": {}, "scope": "all"})
    assert r.status_code == 403, r.text
    assert "scoped to a different app" in r.json()["detail"]


def test_scoped_token_denied_on_non_app_routes(client, admin_token, two_apps):
    """Deny-by-default: a scoped token (even carrying admin identity) must not
    read global list endpoints that have no {app_id} and aren't role-gated —
    /api/apps, /api/ai/providers, /api/datasets/discoverable. Before this,
    wiring an anonymous embed-guest token made these anonymously readable."""
    app_a, _ = two_apps
    preview = _preview_token(admin_token, app_a)

    for path in ("/api/apps", "/api/ai/providers", "/api/datasets/discoverable"):
        # Control: the admin's LOGIN token reads it fine.
        assert client.get(path, headers=_auth(admin_token)).status_code == 200, path
        # The scoped token is denied.
        assert client.get(path, headers=_auth(preview)).status_code == 403, path

    # But the one allowlisted non-app route (fetchUser) still works.
    assert client.get("/api/auth/me", headers=_auth(preview)).status_code == 200


def test_login_tokens_are_not_app_scoped(client, admin_token, two_apps):
    app_a, app_b = two_apps
    for app_id in (app_a, app_b):
        r = client.post(f"/api/apps/{app_id}/db/migrate", headers=_auth(admin_token), json={
            "migrations": [{"version": 1, "name": "t",
                            "sql": "CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)"}]})
        assert r.status_code == 200, r.text


# ---- the proxy mints scoped tokens ------------------------------------------

class _FakeResp:
    def __init__(self):
        self.content = b"<html><head></head><body>app shell</body></html>"
        self.status_code = 200
        self.headers = httpx.Headers({"content-type": "text/html; charset=utf-8"})


class _FakeClient:
    async def request(self, **kwargs):
        return _FakeResp()


def _injected(html: str, key: str) -> str | None:
    m = re.search(rf'window\.__AIHUB_{key}__ = (".*?"|\{{.*?\}});', html)
    if not m:
        return None
    return json.loads(m.group(1))


def test_proxy_injection_is_scoped_and_carries_username(client, admin_token, two_apps, monkeypatch):
    app_a, _ = two_apps
    monkeypatch.setattr(runtime_manager, "get_status",
                        lambda _id: SimpleNamespace(status="running", port=9999))
    monkeypatch.setattr(proxy_mod, "_get_client", lambda: _FakeClient())

    r = client.get(f"/apps/{app_a}/?__aihub_token={admin_token}")
    assert r.status_code == 200
    tok = _injected(r.text, "TOKEN")
    payload = auth_service.decode_access_token(tok)
    assert payload["purpose"] == "preview"
    assert payload["app_id"] == app_a
    assert payload["username"] == "admin"
    user = _injected(r.text, "USER")
    assert user["username"] == "admin"


# ---- embed transport end to end ---------------------------------------------

def test_embed_bootstrap_to_scoped_guest_injection(client, admin_token, two_apps, monkeypatch):
    app_a, app_b = two_apps

    # Enable embedding and fetch the public bootstrap.
    r = client.put(f"/api/apps/{app_a}/embed-config",
                   json={"enabled": True, "allowed_origins": []}, headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    r = client.get(f"/api/apps/{app_a}/embed")
    assert r.status_code == 200
    m = re.search(r"__aihub_embed=([A-Za-z0-9._\-]+)", r.text)
    assert m, "bootstrap iframe URL carries no embed token"
    embed_token = m.group(1)

    # The proxy turns it into an injected, scoped GUEST session token.
    monkeypatch.setattr(runtime_manager, "get_status",
                        lambda _id: SimpleNamespace(status="running", port=9999))
    monkeypatch.setattr(proxy_mod, "_get_client", lambda: _FakeClient())
    r = client.get(f"/apps/{app_a}/view?__aihub_embed={embed_token}")
    assert r.status_code == 200
    tok = _injected(r.text, "TOKEN")
    assert tok, "no token injected for a valid embed credential"
    payload = auth_service.decode_access_token(tok)
    assert payload["purpose"] == "embed"
    assert payload["app_id"] == app_a
    assert payload["role"] == "user"
    assert payload["username"] == "embed-guest"

    # The guest token works on ITS app's SDK routes...
    r = client.post(f"/api/apps/{app_a}/db/query", headers=_auth(tok),
                    json={"sql": "SELECT 1 AS x", "params": {}, "scope": "all"})
    assert r.status_code == 200, r.text
    # ...but not on another app, and never on role-gated routes.
    r = client.post(f"/api/apps/{app_b}/db/query", headers=_auth(tok),
                    json={"sql": "SELECT 1 AS x", "params": {}, "scope": "all"})
    assert r.status_code == 403
    r = client.get(f"/api/apps/{app_a}/embed-config", headers=_auth(tok))
    assert r.status_code == 403

    # A forged/foreign embed credential injects nothing.
    r = client.get(f"/apps/{app_b}/view?__aihub_embed={embed_token}")
    assert r.status_code == 200
    assert _injected(r.text, "TOKEN") is None


# ---- chat WS typed auth errors ----------------------------------------------

def test_ws_rejects_scoped_tokens_with_typed_auth_error(client, admin_token, two_apps):
    app_a, _ = two_apps
    preview = _preview_token(admin_token, app_a)
    with client.websocket_connect("/api/ai/chat") as ws:
        ws.send_json({"token": preview})
        msg = ws.receive_json()
        assert msg["type"] == "auth_error"
        assert msg["data"]["code"] == "token_scope"


def test_ws_invalid_token_gets_typed_auth_error(client):
    with client.websocket_connect("/api/ai/chat") as ws:
        ws.send_json({"token": "not-a-jwt"})
        msg = ws.receive_json()
        assert msg["type"] == "auth_error"
        assert msg["data"]["code"] == "token_invalid"


def test_ws_deactivated_account_is_cut_off(client, admin_token):
    """A user deactivated by an admin must lose chat access immediately —
    at reconnect AND per message on an already-open socket."""
    from sqlalchemy import select

    from src.auth.models import User
    from src.database import async_session

    # A throwaway local user we can deactivate without breaking other tests.
    async def _make_user():
        async with async_session() as db:
            u = User(username=f"doomed-{uuid.uuid4().hex[:6]}",
                     display_name="Doomed", role="developer")
            db.add(u)
            await db.commit()
            await db.refresh(u)
            return u.id, u.username

    user_id, username = asyncio.run(_make_user())
    token = auth_service.create_access_token(user_id, "developer",
                                             extra_claims={"username": username})

    async def _deactivate():
        async with async_session() as db:
            u = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
            u.is_active = False
            await db.commit()

    # Open socket, authenticate, THEN deactivate, then try to chat.
    with client.websocket_connect("/api/ai/chat") as ws:
        ws.send_json({"token": token})
        assert ws.receive_json()["type"] == "authenticated"
        asyncio.run(_deactivate())
        ws.send_json({"app_id": "any", "message": "hello"})
        msg = ws.receive_json()
        assert msg["type"] == "auth_error"
        assert msg["data"]["code"] == "account_disabled"

    # And a fresh connect is rejected at the handshake.
    with client.websocket_connect("/api/ai/chat") as ws:
        ws.send_json({"token": token})
        msg = ws.receive_json()
        assert msg["type"] == "auth_error"
        assert msg["data"]["code"] == "account_disabled"
