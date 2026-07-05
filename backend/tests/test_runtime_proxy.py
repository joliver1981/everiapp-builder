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


def _assert_framable_retry_page(r):
    """The error page must render INSIDE the cross-origin builder iframe and
    heal itself. A JSON/plain-text error gets X-Frame-Options SAMEORIGIN from
    SecurityHeadersMiddleware and Chrome shows a silent blank iframe instead —
    the 'blank preview until Reload iframe' bug."""
    assert r.status_code == 502
    assert r.headers["content-type"].startswith("text/html")
    assert "frame-ancestors" in r.headers.get("content-security-policy", "")
    assert "x-frame-options" not in {k.lower() for k in r.headers.keys()}
    assert "retries automatically" in r.text          # the self-heal script is present
    assert "no-store" in r.headers.get("cache-control", "")


def test_proxy_502_when_app_not_running(client, monkeypatch):
    monkeypatch.setattr(runtime_manager, "get_status", lambda _id: None)
    r = client.get("/apps/not-running-app/")
    _assert_framable_retry_page(r)


def test_proxy_retry_page_on_connect_error(client, monkeypatch):
    """Runtime says 'running' but nothing answers on the port (crashed Vite,
    lost race) — the ConnectError path must serve the framable retry page,
    not the old plain-text 502."""
    import socket

    # A port that is guaranteed closed: bind, note the number, release it.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead_port = s.getsockname()[1]
    s.close()

    monkeypatch.setattr(
        runtime_manager, "get_status",
        lambda _id: SimpleNamespace(status="running", port=dead_port),
    )
    r = client.get("/apps/dead-port-app/")
    _assert_framable_retry_page(r)


def test_proxy_retry_page_on_unexpected_httpx_error(client, monkeypatch):
    """Anything httpx can raise (cold-start ReadTimeout was the killer) must be
    caught and turned into the retry page — it used to escape as a 500."""
    class _BoomClient:
        async def request(self, **kwargs):
            raise httpx.ReadTimeout("simulated cold-start stall")

    monkeypatch.setattr(
        runtime_manager, "get_status",
        lambda _id: SimpleNamespace(status="running", port=9999),
    )
    monkeypatch.setattr(proxy_mod, "_get_client", lambda: _BoomClient())
    r = client.get("/apps/stalled-app/")
    _assert_framable_retry_page(r)


# ---- readiness probe: 'running' must mean the app document actually serves ----

def test_wait_for_ready_requires_app_base_path_200():
    """Vite runs with base=/apps/{id}/ so '/' is its 404 hint page. The old
    probe polled '/' and accepted any <500, declaring 'running' before the
    iframe's first request could succeed. Now readiness = 200 from the real
    base path."""
    import http.server
    import threading

    app_id = "ready-check-app"

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == f"/apps/{app_id}/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html></html>")
            else:
                # everything else (incl. '/') 404s, like Vite's base hint page
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        # The app whose base path serves 200 is ready...
        assert asyncio.run(runtime_manager._wait_for_ready(port, app_id, timeout=5)) is True
        # ...but a 404 (old probe: any <500 on '/') must NOT count as ready.
        assert asyncio.run(runtime_manager._wait_for_ready(port, "other-app", timeout=1)) is False
    finally:
        srv.shutdown()


# ---- websocket relay: vite-hmr subprotocol must survive both hops ----

def test_ws_proxy_negotiates_vite_hmr_subprotocol(client, monkeypatch):
    """Vite's HMR client connects with the 'vite-hmr' subprotocol; per spec the
    browser kills the connection if the 101 doesn't echo it back. The relay
    must forward the requested subprotocol upstream and echo Vite's pick to
    the client — the fake Vite below only greets when it negotiated vite-hmr,
    exactly like the real one."""
    import json
    import threading

    holder: dict = {}
    ready = threading.Event()

    async def handler(conn):
        await conn.send(json.dumps({"type": "connected", "subprotocol": conn.subprotocol}))
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            pass

    def run_server():
        import websockets

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def main():
            server = await websockets.serve(
                handler, "127.0.0.1", 0, subprotocols=["vite-hmr"]
            )
            holder["port"] = server.sockets[0].getsockname()[1]
            holder["stop"] = loop.create_future()
            ready.set()
            await holder["stop"]
            server.close()
            await server.wait_closed()

        loop.run_until_complete(main())
        loop.close()

    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    assert ready.wait(5), "fake Vite HMR server did not start"

    monkeypatch.setattr(
        runtime_manager, "get_status",
        lambda _id: SimpleNamespace(status="running", port=holder["port"]),
    )
    try:
        with client.websocket_connect("/apps/hmr-app/", subprotocols=["vite-hmr"]) as ws:
            # The 101 must echo the negotiated subprotocol back to the browser…
            assert ws.accepted_subprotocol == "vite-hmr"
            # …and upstream (fake Vite) must have seen it too, or it stays mute.
            msg = json.loads(ws.receive_text())
            assert msg == {"type": "connected", "subprotocol": "vite-hmr"}
    finally:
        holder["stop"].get_loop().call_soon_threadsafe(
            lambda: holder["stop"].done() or holder["stop"].set_result(None)
        )
        t.join(timeout=5)


def test_ws_proxy_relays_oversized_frames(client, monkeypatch):
    """The relay must be transparent to frame size. The websockets client's
    default 1MB max_size closed the upstream socket (1009) on a big HMR
    payload — and Vite's client answers ANY reconnect with location.reload(),
    so the running preview 'randomly restarted' mid-session."""
    import json
    import threading

    big = "x" * (1_500_000)  # > the 1MB library default
    holder: dict = {}
    ready = threading.Event()

    async def handler(conn):
        await conn.send(json.dumps({"type": "big-follows"}))
        await conn.send(big)
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            pass

    def run_server():
        import websockets

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def main():
            server = await websockets.serve(
                handler, "127.0.0.1", 0, subprotocols=["vite-hmr"], max_size=None
            )
            holder["port"] = server.sockets[0].getsockname()[1]
            holder["stop"] = loop.create_future()
            ready.set()
            await holder["stop"]
            server.close()
            await server.wait_closed()

        loop.run_until_complete(main())
        loop.close()

    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    assert ready.wait(5), "fake Vite HMR server did not start"

    monkeypatch.setattr(
        runtime_manager, "get_status",
        lambda _id: SimpleNamespace(status="running", port=holder["port"]),
    )
    try:
        with client.websocket_connect("/apps/big-frame-app/", subprotocols=["vite-hmr"]) as ws:
            assert json.loads(ws.receive_text()) == {"type": "big-follows"}
            received = ws.receive_text()
            assert len(received) == len(big)  # arrived intact, socket not killed
    finally:
        holder["stop"].get_loop().call_soon_threadsafe(
            lambda: holder["stop"].done() or holder["stop"].set_result(None)
        )
        t.join(timeout=5)
