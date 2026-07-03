"""Runtime reverse-proxy: SDK-context injection so the builder Preview makes
useDataset/useAppDB work.

Proves:
  - _inject_context (pure) injects window.__AIHUB_APP_ID__ / __AIHUB_TOKEN__ / __AIHUB_USER__
    after <head>, and omits token/user when absent;
  - the real /apps/{id}/ proxy route reads the dev's token from the ?__aihub_token= query param
    (the Preview iframe can't send an Authorization header) and injects it into the served HTML.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_runtime_proxy.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_runtime_proxy")
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "runtime-proxy-test")

import asyncio  # noqa: E402

from src.database import init_db  # noqa: E402
from src.main import app  # noqa: E402
from src.runtime import proxy as proxy_mod  # noqa: E402
from src.runtime.manager import runtime_manager  # noqa: E402


# ---- pure _inject_context ----

def test_inject_context_injects_all_globals_after_head():
    html = "<html><head></head><body>hi</body></html>"
    out = proxy_mod._inject_context(html, "app-123", {"id": "u1", "username": "dev"}, "tok-abc")
    assert 'window.__AIHUB_APP_ID__ = "app-123";' in out
    assert 'window.__AIHUB_TOKEN__ = "tok-abc";' in out
    assert '"username": "dev"' in out                 # user serialized into __AIHUB_USER__
    assert out.index("<script>") > out.index("<head>")   # injected AFTER <head>


def test_inject_context_omits_token_and_user_when_absent():
    out = proxy_mod._inject_context("<head></head>", "app-1", None, None)
    assert 'window.__AIHUB_APP_ID__ = "app-1";' in out
    assert "__AIHUB_TOKEN__" not in out
    assert "__AIHUB_USER__" not in out


# ---- real /apps/{id}/ route: token via query param ----

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


class _FakeResp:
    """Minimal stand-in for the Vite dev server's HTML response."""
    def __init__(self):
        self.content = b"<html><head></head><body>app shell</body></html>"
        self.status_code = 200
        self.headers = httpx.Headers({"content-type": "text/html; charset=utf-8"})


class _FakeClient:
    last_url = None

    async def request(self, **kwargs):
        _FakeClient.last_url = kwargs.get("url")
        return _FakeResp()


def test_proxy_injects_app_id_and_token_from_query_param(client, admin_token, monkeypatch):
    app_id = "preview-app-xyz"
    # Pretend the app's Vite dev server is up...
    monkeypatch.setattr(runtime_manager, "get_status", lambda _id: SimpleNamespace(status="running", port=9999))
    # ...and short-circuit the upstream fetch to our fake HTML.
    monkeypatch.setattr(proxy_mod, "_get_client", lambda: _FakeClient())

    r = client.get(f"/apps/{app_id}/?__aihub_token={admin_token}")
    assert r.status_code == 200, r.text
    body = r.text
    assert f'window.__AIHUB_APP_ID__ = "{app_id}";' in body   # the SDK gets its app id
    assert f'window.__AIHUB_TOKEN__ = "{admin_token}";' in body  # ...and an auth token
    assert "__AIHUB_USER__" in body                            # decoded from the token
    # Forwarded the FULL base-prefixed path to Vite (base=/apps/{id}/), not a stripped one —
    # else every asset (/@vite/client, /src/main.tsx) 404s and the app never boots.
    assert _FakeClient.last_url.startswith(f"http://127.0.0.1:9999/apps/{app_id}/")


def test_proxy_injects_token_from_cookie(client, admin_token, monkeypatch):
    """The app VIEWER's transport: an `access_token` cookie scoped to /apps —
    keeps the bearer token out of the URL (address bar, history, access logs)."""
    app_id = "viewer-app-cookie"
    monkeypatch.setattr(runtime_manager, "get_status", lambda _id: SimpleNamespace(status="running", port=9999))
    monkeypatch.setattr(proxy_mod, "_get_client", lambda: _FakeClient())

    r = client.get(f"/apps/{app_id}/", cookies={"access_token": admin_token})
    assert r.status_code == 200, r.text
    assert f'window.__AIHUB_TOKEN__ = "{admin_token}";' in r.text
    assert f'window.__AIHUB_APP_ID__ = "{app_id}";' in r.text


def test_proxy_never_injects_undecodable_tokens(client, admin_token, monkeypatch):
    """Expired/garbage tokens are dropped, and a bad transport must not shadow
    a valid one further down the chain (bad cookie + good query param)."""
    monkeypatch.setattr(runtime_manager, "get_status", lambda _id: SimpleNamespace(status="running", port=9999))
    monkeypatch.setattr(proxy_mod, "_get_client", lambda: _FakeClient())

    r = client.get("/apps/junk-token-app/?__aihub_token=not-a-jwt")
    assert r.status_code == 200
    assert "__AIHUB_TOKEN__" not in r.text
    assert "__AIHUB_USER__" not in r.text

    r = client.get(f"/apps/junk-token-app/?__aihub_token={admin_token}",
                   cookies={"access_token": "expired-garbage"})
    assert r.status_code == 200
    assert f'window.__AIHUB_TOKEN__ = "{admin_token}";' in r.text


def test_proxy_502_when_app_not_running(client, monkeypatch):
    monkeypatch.setattr(runtime_manager, "get_status", lambda _id: None)
    r = client.get("/apps/not-running-app/")
    assert r.status_code == 502
