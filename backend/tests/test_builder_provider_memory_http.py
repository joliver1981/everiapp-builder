"""Per-app builder-provider memory — via the real chat WebSocket route.

The builder's provider dropdown used to reset to the platform default on
every mount, silently switching models mid-build. Now the provider a chat
message actually USES is persisted on the app (only after it resolves — bogus
ids never stick) and returned by GET /api/apps/{id} for the dropdown to
restore. Falls back to the default client-side when the remembered provider
was since deleted (the id just stops matching /ai/providers).
"""
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.main import app

client_ctx = None


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


def _mk_chunk(text: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text))],
        usage=None,
    )


@pytest.fixture()
def fake_llm(monkeypatch):
    """Stub the LLM boundary: the real chat pipeline runs, no network."""
    async def fake_acompletion(**kwargs):
        async def gen():
            yield _mk_chunk("Hello! ")
            yield _mk_chunk("I made no file changes.")
        return gen()
    monkeypatch.setattr("src.ai.service.acompletion", fake_acompletion)


def _chat_roundtrip(client: TestClient, token: str, app_id: str,
                    provider_id: str | None) -> list[dict]:
    """Authenticate the chat WS, send one message, drain until done/error."""
    received: list[dict] = []
    with client.websocket_connect("/api/ai/chat") as ws:
        ws.send_json({"token": token})
        first = ws.receive_json()
        assert first["type"] == "authenticated", first
        payload = {"app_id": app_id, "message": "hello there"}
        if provider_id is not None:
            payload["provider_id"] = provider_id
        ws.send_json(payload)
        for _ in range(60):  # drain bound — a turn is a handful of chunks
            msg = ws.receive_json()
            received.append(msg)
            if msg["type"] in ("done", "error"):
                break
    return received


def test_chat_persists_the_resolved_provider(client, admin_token, fake_llm):
    r = client.post("/api/admin/ai-providers", headers=_auth(admin_token), json={
        "name": f"prov-{uuid.uuid4().hex[:6]}",
        "provider_type": "anthropic",
        "api_key": "sk-test-not-real",
        "default_model": "claude-fable-5",
    })
    assert r.status_code in (200, 201), r.text
    provider_id = r.json()["id"]

    r = client.post("/api/apps", json={"name": f"provmem-{uuid.uuid4().hex[:6]}"},
                    headers=_auth(admin_token))
    assert r.status_code in (200, 201), r.text
    app_id = r.json()["id"]
    assert r.json().get("builder_provider_id") is None

    received = _chat_roundtrip(client, admin_token, app_id, provider_id)
    assert any(m["type"] == "done" for m in received), received

    r = client.get(f"/api/apps/{app_id}", headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    assert r.json()["builder_provider_id"] == provider_id

    # A bogus provider id fails the turn and must NOT overwrite the memory.
    received = _chat_roundtrip(client, admin_token, app_id, "not-a-real-provider")
    assert any(m["type"] == "error" for m in received), received
    r = client.get(f"/api/apps/{app_id}", headers=_auth(admin_token))
    assert r.json()["builder_provider_id"] == provider_id
