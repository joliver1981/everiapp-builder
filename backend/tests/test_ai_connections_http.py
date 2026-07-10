"""First-class AI Provider connections (kind="ai").

Real HTTP routes (TestClient, real auth); outbound httpx faked at the
rest-driver seam. Locks in the turnkey contract: the preset registry serves
known base URLs/auth so an admin never looks one up, ai configs are validated
eagerly (unlike sql/rest), fetch-models pulls a live model list before the row
exists, test-connection REALLY validates the API key (a 401 is a failure, not
"Connection successful" like _test_rest), and apps see provider/models/
default_model through the same guardrailed surface callConnection already uses.
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
_DB = _TMP / "test_ai_connections.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_ai_connections")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "ai-connections-test")

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


# --- fake httpx client at the rest-driver seam -------------------------------
# app_calls consumes the streaming shape (.stream); the models fetch and the
# ai-kind connection test use plain .get — the fake covers both.
class _FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self.headers = {"content-type": "application/json"}
        self.encoding = "utf-8"
        self._payload = payload if payload is not None else {"ok": True}
        self._body = json.dumps(self._payload).encode()

    def json(self):
        return self._payload

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
    def __init__(self, sink, get_resp: _FakeResp):
        self._sink = sink
        self._get_resp = get_resp

    def stream(self, method, path, **kwargs):
        self._sink["method"] = method
        self._sink["path"] = path
        self._sink["kwargs"] = kwargs
        return _FakeStream(_FakeResp(payload={"echo": path}))

    async def get(self, path, **kwargs):
        self._sink["get_path"] = path
        self._sink["get_kwargs"] = kwargs
        return self._get_resp

    async def aclose(self):
        pass


@pytest.fixture()
def captured(monkeypatch):
    """Fake outbound HTTP; set sink['get_response'] BEFORE the request to shape
    what the provider's models endpoint answers."""
    sink: dict = {"get_response": _FakeResp(payload={"data": [{"id": "model-a"}]})}

    def fake_build_client(config, *, secret=None, timeout_seconds=30):
        sink["config"] = config
        sink["secret"] = secret
        return _FakeClient(sink, sink["get_response"])

    monkeypatch.setattr(rest_driver, "build_client", fake_build_client)
    return sink


_ANTHROPIC_CONFIG = {
    "provider": "anthropic",
    "base_url": "https://api.anthropic.com/v1",
    "auth_type": "none",  # keep the secret store out of these tests
    "default_headers": {"anthropic-version": "2023-06-01"},
    "default_query": {},
    "models": ["claude-sonnet-5", "claude-haiku-4-5"],
    "default_model": "claude-sonnet-5",
}


def _make_ai_conn(client, admin, *, config=None, app_callable=True) -> dict:
    r = client.post("/api/admin/connections", json={
        "name": f"ai-{uuid.uuid4().hex[:6]}",
        "kind": "ai",
        "config": config if config is not None else dict(_ANTHROPIC_CONFIG),
        "app_callable": app_callable,
    }, headers=admin)
    assert r.status_code in (200, 201), r.text
    return r.json()


def _make_app(client, admin) -> str:
    return client.post("/api/apps", json={"name": f"ai-{uuid.uuid4().hex[:6]}"}, headers=admin).json()["id"]


def _bind(client, admin, app_id, conn_id):
    r = client.post(f"/api/apps/{app_id}/connections/{conn_id}", headers=admin)
    assert r.status_code in (200, 201), r.text


def _scoped_headers(admin, app_id):
    raw = admin["Authorization"].split(" ", 1)[1]
    payload = auth_service.decode_access_token(raw)
    tok = auth_service.create_access_token(
        payload["sub"], payload["role"], expire_minutes=60,
        extra_claims={"purpose": "preview", "app_id": app_id,
                      "username": payload.get("username", "")},
    )
    return {"Authorization": f"Bearer {tok}"}


# --- preset registry ---------------------------------------------------------

def test_preset_registry_serves_known_provider_defaults(client, admin):
    """The whole point of first-class AI connections: the admin never has to
    know a base URL or auth convention — the registry carries them."""
    r = client.get("/api/admin/connections/ai-providers", headers=admin)
    assert r.status_code == 200, r.text
    by_key = {p["provider"]: p for p in r.json()["providers"]}
    for key in ("openai", "anthropic", "openrouter", "azure_openai", "custom"):
        assert key in by_key, f"missing preset {key}"
        preset = by_key[key]
        for field in ("label", "base_url", "auth_type", "models_path",
                      "chat_path", "api_format", "suggested_models", "hint"):
            assert field in preset, f"{key} missing {field}"

    assert by_key["openai"]["base_url"] == "https://api.openai.com/v1"
    assert by_key["openai"]["auth_type"] == "bearer"
    # Anthropic's two non-obvious conventions must be baked in.
    assert by_key["anthropic"]["auth_type"] == "api_key_header"
    assert by_key["anthropic"]["auth_param"] == "x-api-key"
    assert "anthropic-version" in by_key["anthropic"]["default_headers"]
    assert by_key["anthropic"]["chat_path"] == "/messages"
    assert by_key["anthropic"]["api_format"] == "anthropic"
    # Azure: api-key header + api-version query param + placeholder to edit.
    assert by_key["azure_openai"]["auth_param"] == "api-key"
    assert "api-version" in by_key["azure_openai"]["default_query"]
    assert "YOUR-RESOURCE" in by_key["azure_openai"]["base_url"]
    assert by_key["openrouter"]["base_url"] == "https://openrouter.ai/api/v1"


def test_registry_requires_admin(client):
    tok = client.post("/api/auth/login", json={"username": "user", "password": "password"}).json()["access_token"]
    r = client.get("/api/admin/connections/ai-providers",
                   headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403


# --- eager config validation (unlike sql/rest) -------------------------------

def test_ai_config_is_validated_at_create_time(client, admin):
    def attempt(config):
        return client.post("/api/admin/connections", json={
            "name": f"bad-{uuid.uuid4().hex[:6]}", "kind": "ai", "config": config,
        }, headers=admin)

    r = attempt({"base_url": "https://api.example.com"})  # no provider
    assert r.status_code == 400 and "provider" in r.json()["detail"]

    r = attempt({"provider": "anthropic"})  # no base_url
    assert r.status_code == 400 and "base_url" in r.json()["detail"]

    r = attempt({"provider": "azure_openai",
                 "base_url": "https://YOUR-RESOURCE.openai.azure.com/openai/v1"})
    assert r.status_code == 400 and "YOUR-RESOURCE" in r.json()["detail"]

    r = attempt({"provider": "anthropic", "base_url": "https://api.anthropic.com/v1",
                 "models": "claude-sonnet-5"})  # models must be a list
    assert r.status_code == 400 and "models" in r.json()["detail"]


def test_ai_config_is_validated_on_update_too(client, admin):
    conn = _make_ai_conn(client, admin)
    r = client.put(f"/api/admin/connections/{conn['id']}",
                   json={"config": {"provider": "nope", "base_url": "https://x.example"}},
                   headers=admin)
    assert r.status_code == 400 and "provider" in r.json()["detail"]


def test_kind_cannot_be_changed_on_update(client, admin):
    """ConnectionUpdate used to silently DROP a changed kind, so a rest→ai
    'migration' in the edit dialog saved an ai-shaped config onto a row still
    labeled rest (skipping ai validation entirely). Now it's an explicit 400;
    echoing the SAME kind back stays fine."""
    conn = _make_ai_conn(client, admin)
    r = client.put(f"/api/admin/connections/{conn['id']}",
                   json={"kind": "rest", "config": {"base_url": "https://x.example"}},
                   headers=admin)
    assert r.status_code == 400
    assert "kind" in r.json()["detail"].lower()

    r = client.put(f"/api/admin/connections/{conn['id']}",
                   json={"kind": "ai", "description": "same kind is fine"}, headers=admin)
    assert r.status_code == 200, r.text


# --- apps can call through an AI connection ----------------------------------

def test_app_can_call_through_an_ai_connection(client, admin, captured):
    """kind="ai" rides the exact callConnection guardrails REST already has —
    the kind gate must admit it, and the preset headers (anthropic-version)
    must reach the outbound client via config."""
    app_id = _make_app(client, admin)
    conn = _make_ai_conn(client, admin)
    _bind(client, admin, app_id, conn["id"])

    r = client.post(f"/api/apps/{app_id}/connections/{conn['id']}/call",
                    json={"method": "POST", "path": "/messages",
                          "body": {"model": "claude-sonnet-5", "messages": []}},
                    headers=admin)
    assert r.status_code == 200, r.text
    assert captured["method"] == "POST"
    assert captured["path"] == "/messages"
    assert captured["config"]["default_headers"]["anthropic-version"] == "2023-06-01"


def test_app_cannot_override_the_api_key_header_credential(client, admin, captured):
    """The AI feature's flagship auth convention is api_key_header (Anthropic
    x-api-key, Azure api-key). httpx merges request headers OVER the client's
    credential header, so _reserved_header_names stripping the auth_param is
    the ONLY thing keeping a generated app from clobbering the platform's
    injected key — lock it in like the bearer/api_key_query tests do."""
    app_id = _make_app(client, admin)
    conn = _make_ai_conn(client, admin, config={
        **_ANTHROPIC_CONFIG, "auth_type": "api_key_header", "auth_param": "x-api-key",
    })
    _bind(client, admin, app_id, conn["id"])

    client.post(f"/api/apps/{app_id}/connections/{conn['id']}/call",
                json={"method": "POST", "path": "/messages",
                      "headers": {"x-api-key": "attacker", "X-Custom": "ok"}},
                headers=admin)
    sent = captured["kwargs"].get("headers", {})
    assert "X-Custom" in sent
    assert not any(k.lower() == "x-api-key" for k in sent)


def test_sql_connection_still_cannot_be_called(client, admin, captured):
    app_id = _make_app(client, admin)
    r = client.post("/api/admin/connections", json={
        "name": f"sql-{uuid.uuid4().hex[:6]}", "kind": "sql",
        "config": {"dialect": "sqlite", "database": ":memory:"},
    }, headers=admin)
    conn_id = r.json()["id"]
    _bind(client, admin, app_id, conn_id)
    r = client.post(f"/api/apps/{app_id}/connections/{conn_id}/call",
                    json={"method": "GET", "path": "/x"}, headers=admin)
    assert r.status_code == 400


# --- provider/models are exposed to apps, secrets are not --------------------

def test_running_app_sees_provider_and_models(client, admin):
    """The SDK's aiChat/useConnections need provider, api_format, models,
    default_model, chat_path — served on the app's own scoped token, with the
    same no-config/no-secret posture the REST list already locks in."""
    app_id = _make_app(client, admin)
    conn = _make_ai_conn(client, admin)
    _bind(client, admin, app_id, conn["id"])
    scoped = _scoped_headers(admin, app_id)

    r = client.get(f"/api/apps/{app_id}/connections", headers=scoped)
    assert r.status_code == 200, r.text
    items = r.json()
    mine = next(c for c in items if c["id"] == conn["id"])
    assert mine["kind"] == "ai"
    assert mine["provider"] == "anthropic"
    assert mine["api_format"] == "anthropic"
    assert mine["models"] == ["claude-sonnet-5", "claude-haiku-4-5"]
    assert mine["default_model"] == "claude-sonnet-5"
    assert mine["chat_path"] == "/messages"  # preset fallback — not in config
    assert "config" not in mine and "secret" not in json.dumps(items).lower()


def test_callable_picklist_includes_ai_connections(client, admin):
    conn = _make_ai_conn(client, admin, app_callable=True)
    not_callable = _make_ai_conn(client, admin, app_callable=False)
    r = client.get("/api/connections/callable", headers=admin)
    assert r.status_code == 200, r.text
    ids = {c["id"]: c for c in r.json()}
    assert conn["id"] in ids
    assert not_callable["id"] not in ids
    entry = ids[conn["id"]]
    assert entry["kind"] == "ai" and entry["provider"] == "anthropic"
    assert entry["models"] == ["claude-sonnet-5", "claude-haiku-4-5"]


# --- fetch-models ------------------------------------------------------------

def test_fetch_models_returns_the_live_list(client, admin, captured):
    captured["get_response"] = _FakeResp(payload={"data": [
        {"id": "claude-opus-4-8"}, {"id": "claude-sonnet-5"}, {"id": "claude-opus-4-8"},
    ]})
    r = client.post("/api/admin/connections/fetch-models", json={
        "config": dict(_ANTHROPIC_CONFIG), "credential_secret_ref": None,
    }, headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["models"] == ["claude-opus-4-8", "claude-sonnet-5"]  # sorted, deduped
    assert captured["get_path"] == "/models"  # anthropic preset's models_path
    # Anthropic paginates at 20/page by default — the preset's models_query
    # must ride along or the fetched list silently truncates.
    assert captured["get_kwargs"].get("params") == {"limit": "1000"}


def test_fetch_models_surfaces_a_rejected_key(client, admin, captured):
    captured["get_response"] = _FakeResp(status=401, payload={"error": "bad key"})
    r = client.post("/api/admin/connections/fetch-models", json={
        "config": dict(_ANTHROPIC_CONFIG), "credential_secret_ref": None,
    }, headers=admin)
    assert r.status_code == 400
    assert "api key" in r.json()["detail"].lower()


def test_fetch_models_names_a_missing_secret(client, admin, captured):
    r = client.post("/api/admin/connections/fetch-models", json={
        "config": dict(_ANTHROPIC_CONFIG),
        "credential_secret_ref": "no-such-secret",
    }, headers=admin)
    assert r.status_code == 400
    assert "no-such-secret" in r.json()["detail"]


# --- test-connection actually validates the key ------------------------------

def test_ai_connection_test_succeeds_on_2xx(client, admin, captured):
    conn = _make_ai_conn(client, admin)
    captured["get_response"] = _FakeResp(payload={"data": [{"id": "m"}]})
    r = client.post(f"/api/admin/connections/{conn['id']}/test", headers=admin)
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_ai_connection_test_passes_on_an_empty_model_list(client, admin, captured):
    """A 2xx with zero models (fresh Azure resource, no deployments yet) means
    the key WORKS — the connection test must pass; only fetch-models treats an
    empty list as an error."""
    conn = _make_ai_conn(client, admin)
    captured["get_response"] = _FakeResp(payload={"data": []})
    r = client.post(f"/api/admin/connections/{conn['id']}/test", headers=admin)
    assert r.json()["success"] is True

    captured["get_response"] = _FakeResp(payload={"data": []})
    r = client.post("/api/admin/connections/fetch-models", json={
        "config": dict(_ANTHROPIC_CONFIG), "credential_secret_ref": None,
    }, headers=admin)
    assert r.status_code == 400


def test_ai_connection_test_fails_on_401_unlike_rest(client, admin, captured):
    """_test_rest treats ANY HTTP response as success; for an AI provider a 401
    means 'your key is wrong' and MUST be a failure the admin sees."""
    conn = _make_ai_conn(client, admin)
    captured["get_response"] = _FakeResp(status=401, payload={"error": "bad key"})
    r = client.post(f"/api/admin/connections/{conn['id']}/test", headers=admin)
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is False
    assert "api key" in body["message"].lower()


def test_ai_connection_test_flags_a_dangling_credential_ref(client, admin, captured):
    """resolve_credential returns None silently for a typo'd secret name — for
    rest kinds the call just goes out unauthenticated. AI kinds must instead
    tell the admin the secret is missing."""
    conn = _make_ai_conn(client, admin, config={
        **_ANTHROPIC_CONFIG, "auth_type": "api_key_header", "auth_param": "x-api-key",
    })
    client.put(f"/api/admin/connections/{conn['id']}",
               json={"credential_secret_ref": "typo-secret-name"}, headers=admin)
    r = client.post(f"/api/admin/connections/{conn['id']}/test", headers=admin)
    body = r.json()
    assert body["success"] is False
    assert "secret" in body["message"].lower()
