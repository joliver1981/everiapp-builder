"""SIEM forwarding: cursor-tracked tail of audit_logs pushed over HTTP.

Uses a real local HTTP collector (127.0.0.1, ephemeral port) so the httpx push
path is genuinely exercised. SIEM settings live in the shared platform_settings
DB, so we reset them in teardown.
"""
from __future__ import annotations

import asyncio
import http.server
import json
import os
import sqlite3
import tempfile
import threading
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_siem.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_siem")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "siem-test")

from src.config import settings  # noqa: E402
from src.database import init_db  # noqa: E402
from src.main import app  # noqa: E402


class _Collector(http.server.BaseHTTPRequestHandler):
    events: list = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        for line in body.splitlines():
            if line.strip():
                try:
                    _Collector.events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module", autouse=True)
def _init():
    asyncio.run(init_db())
    yield


@pytest.fixture(scope="module")
def collector():
    _Collector.events = []
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Collector)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield port
    srv.shutdown()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin_token(client):
    return client.post("/api/auth/login", json={"username": "admin", "password": "password"}).json()["access_token"]


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture(scope="module", autouse=True)
def _reset_siem_after(client, admin_token):
    yield
    client.put("/api/admin/settings",
               json={"siem_enabled": False, "siem_endpoint": "", "siem_transport": "http"},
               headers=_auth(admin_token))


def _insert_audit(n: int, action: str, offset_seconds: int):
    db_path = settings.database_url[len("sqlite+aiosqlite:///"):]
    conn = sqlite3.connect(db_path)
    try:
        for i in range(n):
            conn.execute(
                "INSERT INTO audit_logs (id, user_id, action, resource_type, resource_id, details, created_at) "
                f"VALUES (?, 'system', ?, 'test', ?, 'x', datetime('now', '+{offset_seconds} seconds'))",
                (str(uuid.uuid4()), action, f"r{i}"),
            )
        conn.commit()
    finally:
        conn.close()


def _drain(client, admin_token, max_iters=30):
    for _ in range(max_iters):
        st = client.get("/api/admin/siem/status", headers=_auth(admin_token)).json()
        if st["pending"] == 0:
            return
        client.post("/api/admin/siem/flush", headers=_auth(admin_token))


def test_http_forwarding_with_cursor(client, admin_token, collector):
    endpoint = f"http://127.0.0.1:{collector}/collect"
    client.put("/api/admin/settings",
               json={"siem_enabled": True, "siem_endpoint": endpoint, "siem_transport": "http"},
               headers=_auth(admin_token))

    # Drain any pre-existing backlog, then forget those events.
    _drain(client, admin_token)
    _Collector.events = []

    # Insert 3 fresh audit rows and flush — exactly 3 should be forwarded.
    _insert_audit(3, "test.siem.alpha", 5)
    r = client.post("/api/admin/siem/flush", headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    assert r.json()["forwarded"] == 3
    alpha = [e for e in _Collector.events if e["action"] == "test.siem.alpha"]
    assert len(alpha) == 3
    assert all(e["source"] == "aihub" for e in alpha)

    # Cursor advanced — nothing pending now.
    assert client.get("/api/admin/siem/status", headers=_auth(admin_token)).json()["pending"] == 0

    # New rows are picked up incrementally; old ones are NOT re-sent.
    _insert_audit(2, "test.siem.beta", 10)
    r = client.post("/api/admin/siem/flush", headers=_auth(admin_token))
    assert r.json()["forwarded"] == 2
    assert len([e for e in _Collector.events if e["action"] == "test.siem.beta"]) == 2
    assert len([e for e in _Collector.events if e["action"] == "test.siem.alpha"]) == 3  # unchanged


def test_test_endpoint_and_disabled(client, admin_token, collector):
    endpoint = f"http://127.0.0.1:{collector}/collect"
    client.put("/api/admin/settings",
               json={"siem_enabled": True, "siem_endpoint": endpoint, "siem_transport": "http"},
               headers=_auth(admin_token))
    _Collector.events = []

    # Connectivity test sends one synthetic event without moving the cursor.
    r = client.post("/api/admin/siem/test", headers=_auth(admin_token))
    assert r.status_code == 200 and r.json()["ok"] is True
    assert any(e["action"] == "siem.test" for e in _Collector.events)

    # Disabled → flush is a no-op.
    client.put("/api/admin/settings", json={"siem_enabled": False}, headers=_auth(admin_token))
    r = client.post("/api/admin/siem/flush", headers=_auth(admin_token))
    assert r.json().get("skipped") == "disabled"


def test_bad_endpoint_returns_502(client, admin_token):
    client.put("/api/admin/settings",
               json={"siem_enabled": True, "siem_endpoint": "http://127.0.0.1:1/nope",
                     "siem_transport": "http"},
               headers=_auth(admin_token))
    r = client.post("/api/admin/siem/test", headers=_auth(admin_token))
    assert r.status_code == 502
