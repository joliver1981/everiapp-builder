"""Phase 1: free-form external calls through a Connection (callConnection).

Real HTTP routes (TestClient, real auth). The outbound httpx client is faked at
the rest-driver seam so no network is touched, which lets us assert the platform
contract: the app supplies method/path/body, the platform gates the call
(app-callable + bound + rest + relative path) and never lets the app override the
injected credential header.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_external_calls.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_external_calls")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "external-calls-test")

from src.auth.service import auth_service  # noqa: E402
from src.database import init_db  # noqa: E402
from src.main import app as fastapi_app  # noqa: E402
import src.connections.drivers.rest as rest_driver  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _init():
    asyncio.run(init_db())
    yield


@pytest.fixture(scope="module")
def client():
    with TestClient(fastapi_app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "password"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# --- fake httpx client at the rest-driver seam (streaming shape) ------------
class _FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self.headers = {"content-type": "application/json"}
        self.encoding = "utf-8"
        self._body = json.dumps(payload if payload is not None else {"ok": True}).encode()

    async def aiter_bytes(self):
        yield self._body


class _FakeStream:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class _FakeClient:
    def __init__(self, sink):
        self._sink = sink

    def stream(self, method, path, **kwargs):
        self._sink["method"] = method
        self._sink["path"] = path
        self._sink["kwargs"] = kwargs
        return _FakeStream(_FakeResp(payload={"echo": path}))

    async def aclose(self):
        pass


@pytest.fixture()
def captured(monkeypatch):
    sink: dict = {}

    def fake_build_client(config, *, secret=None, timeout_seconds=30):
        sink["config"] = config
        sink["secret"] = secret
        return _FakeClient(sink)

    monkeypatch.setattr(rest_driver, "build_client", fake_build_client)
    return sink


def _make_conn(client, admin, *, app_callable=True, kind="rest") -> str:
    r = client.post("/api/admin/connections", json={
        "name": f"conn-{uuid.uuid4().hex[:6]}",
        "kind": kind,
        "config": {"base_url": "https://api.example.com", "auth_type": "bearer"},
        "app_callable": app_callable,
    }, headers=admin)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _make_app(client, admin) -> str:
    return client.post("/api/apps", json={"name": f"call-{uuid.uuid4().hex[:6]}"}, headers=admin).json()["id"]


def _bind(client, admin, app_id, conn_id):
    r = client.post(f"/api/apps/{app_id}/connections/{conn_id}", headers=admin)
    assert r.status_code in (200, 201), r.text


def test_bound_callable_connection_makes_the_call(client, admin, captured):
    app_id = _make_app(client, admin)
    conn_id = _make_conn(client, admin, app_callable=True)
    _bind(client, admin, app_id, conn_id)

    r = client.post(f"/api/apps/{app_id}/connections/{conn_id}/call",
                    json={"method": "POST", "path": "/v1/messages", "body": {"hi": 1}},
                    headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == 200
    assert body["body"] == {"echo": "/v1/messages"}
    # The app's method/path/body reached the (faked) upstream client.
    assert captured["method"] == "POST"
    assert captured["path"] == "/v1/messages"
    assert captured["kwargs"].get("json") == {"hi": 1}


def test_app_cannot_override_the_injected_auth_header(client, admin, captured):
    app_id = _make_app(client, admin)
    conn_id = _make_conn(client, admin, app_callable=True)
    _bind(client, admin, app_id, conn_id)

    client.post(f"/api/apps/{app_id}/connections/{conn_id}/call",
                json={"method": "GET", "path": "/x",
                      "headers": {"Authorization": "Bearer HACK", "X-Custom": "ok"}},
                headers=admin)
    sent = captured["kwargs"].get("headers", {})
    assert "X-Custom" in sent                       # ordinary header passes through
    assert not any(k.lower() == "authorization" for k in sent)  # credential header stripped


def test_api_key_query_credential_cannot_be_overridden(client, admin, captured):
    """For api_key_query auth the credential rides in a query param — the app
    must NOT be able to override/blank it. The app's `api_key` query key is
    stripped so build_client's injected secret survives; other params pass."""
    app_id = _make_app(client, admin)
    r = client.post("/api/admin/connections", json={
        "name": f"kq-{uuid.uuid4().hex[:6]}", "kind": "rest",
        "config": {"base_url": "https://api.example.com", "auth_type": "api_key_query"},
        "app_callable": True,
    }, headers=admin)
    conn_id = r.json()["id"]
    _bind(client, admin, app_id, conn_id)

    client.post(f"/api/apps/{app_id}/connections/{conn_id}/call",
                json={"method": "GET", "path": "/data",
                      "query": {"api_key": "attacker", "q": "keep"}},
                headers=admin)
    params = captured["kwargs"].get("params", {})
    assert "api_key" not in params            # credential param stripped
    assert params.get("q") == "keep"          # ordinary param passes through


def test_bind_requires_a_privileged_session(client, captured):
    """Binding a connection is a builder action — a plain 'user' role (and, by
    extension, a running app's preview/embed scoped token) is rejected."""
    tok = client.post("/api/auth/login", json={"username": "user", "password": "password"}).json()["access_token"]
    uhdr = {"Authorization": f"Bearer {tok}"}
    r = client.post("/api/apps/some-app/connections/some-conn", headers=uhdr)
    assert r.status_code == 403


def test_unbound_app_is_denied(client, admin, captured):
    app_id = _make_app(client, admin)
    conn_id = _make_conn(client, admin, app_callable=True)  # NOT bound
    r = client.post(f"/api/apps/{app_id}/connections/{conn_id}/call",
                    json={"method": "GET", "path": "/x"}, headers=admin)
    assert r.status_code == 403
    # The message must tell the user how to fix it (attach in the builder).
    assert "attach" in r.json()["detail"].lower()


def test_connection_resolves_by_name_or_id(client, admin, captured):
    """An app can reference a connection by its human-readable NAME, not just the
    UUID id — bindings are still stored/checked by the canonical id."""
    app_id = _make_app(client, admin)
    r = client.post("/api/admin/connections", json={
        "name": f"named-{uuid.uuid4().hex[:6]}", "kind": "rest",
        "config": {"base_url": "https://api.example.com", "auth_type": "none"},
        "app_callable": True,
    }, headers=admin)
    name, cid = r.json()["name"], r.json()["id"]

    # Bind BY NAME — the stored binding uses the canonical UUID.
    assert client.post(f"/api/apps/{app_id}/connections/{name}", headers=admin).status_code in (200, 201)
    bound = client.get(f"/api/apps/{app_id}/connections", headers=admin).json()
    assert any(b["id"] == cid for b in bound)

    # Call BY NAME and BY ID both work.
    for ident in (name, cid):
        rr = client.post(f"/api/apps/{app_id}/connections/{ident}/call",
                         json={"method": "GET", "path": "/x"}, headers=admin)
        assert rr.status_code == 200, f"{ident}: {rr.text}"


def test_non_app_callable_connection_is_denied(client, admin, captured):
    app_id = _make_app(client, admin)
    conn_id = _make_conn(client, admin, app_callable=False)
    _bind(client, admin, app_id, conn_id)
    r = client.post(f"/api/apps/{app_id}/connections/{conn_id}/call",
                    json={"method": "GET", "path": "/x"}, headers=admin)
    assert r.status_code == 403
    assert "not app-callable" in r.json()["detail"].lower()


def test_absolute_path_is_rejected(client, admin, captured):
    app_id = _make_app(client, admin)
    conn_id = _make_conn(client, admin, app_callable=True)
    _bind(client, admin, app_id, conn_id)
    r = client.post(f"/api/apps/{app_id}/connections/{conn_id}/call",
                    json={"method": "GET", "path": "https://evil.example.com/steal"},
                    headers=admin)
    assert r.status_code == 400
    assert "relative" in r.json()["detail"].lower()


def test_delete_app_with_a_connection_binding(client, admin):
    """Deleting an app that's bound to a connection must succeed — the binding's
    FK to apps has no cascade, so delete_app has to clear it first (else 500)."""
    app_id = _make_app(client, admin)
    conn_id = _make_conn(client, admin, app_callable=True)
    _bind(client, admin, app_id, conn_id)
    assert client.delete(f"/api/apps/{app_id}", headers=admin).status_code == 204
    # The connection itself survives; only the binding is gone.
    assert client.get(f"/api/admin/connections/{conn_id}", headers=admin).status_code == 200


def test_delete_connection_with_a_binding(client, admin):
    """Deleting a connection an app is bound to must succeed (binding cleared)."""
    app_id = _make_app(client, admin)
    conn_id = _make_conn(client, admin, app_callable=True)
    _bind(client, admin, app_id, conn_id)
    assert client.delete(f"/api/admin/connections/{conn_id}", headers=admin).status_code == 204
    # The app's bound-connection list no longer includes it.
    assert client.get(f"/api/apps/{app_id}/connections", headers=admin).json() == []


def _scoped_headers(admin, app_id):
    """Mint what the runtime proxy injects into a running app as
    window.__AIHUB_TOKEN__ — a preview-purpose token scoped to this app."""
    raw = admin["Authorization"].split(" ", 1)[1]
    payload = auth_service.decode_access_token(raw)
    tok = auth_service.create_access_token(
        payload["sub"], payload["role"], expire_minutes=60,
        extra_claims={"purpose": "preview", "app_id": app_id,
                      "username": payload.get("username", "")},
    )
    return {"Authorization": f"Bearer {tok}"}


def test_running_app_can_list_its_own_connections(client, admin):
    """Runtime discovery — the SDK's useConnections()/listConnections() hits
    GET /api/apps/{id}/connections with the app's OWN scoped token. This is what
    lets a generated app render 'one card per attached provider' without a
    hardcoded registry file, so lock in: (a) the scoped token is accepted,
    (b) the response carries the fields the SDK maps (id/name/description/
    base_url/app_callable), (c) no secrets leak, (d) another app's scoped
    token is rejected."""
    app_id = _make_app(client, admin)
    conn_id = _make_conn(client, admin, app_callable=True)
    _bind(client, admin, app_id, conn_id)
    scoped = _scoped_headers(admin, app_id)

    r = client.get(f"/api/apps/{app_id}/connections", headers=scoped)
    assert r.status_code == 200, r.text
    items = r.json()
    mine = next(c for c in items if c["id"] == conn_id)
    for field in ("id", "name", "description", "base_url", "app_callable"):
        assert field in mine
    assert mine["base_url"] == "https://api.example.com"
    assert mine["app_callable"] is True
    assert "config" not in mine and "secret" not in json.dumps(items).lower()

    # Scoped containment: a token minted for a DIFFERENT app can't read this list.
    other = _scoped_headers(admin, "some-other-app")
    assert client.get(f"/api/apps/{app_id}/connections", headers=other).status_code == 403


def test_only_callable_connections_are_discoverable(client, admin):
    _make_conn(client, admin, app_callable=True)
    _make_conn(client, admin, app_callable=False)
    r = client.get("/api/connections/callable", headers=admin)
    assert r.status_code == 200, r.text
    assert r.json(), "at least one app-callable connection should be listed"
    assert all("base_url" in c for c in r.json())
