"""Transport hardening + runtime rate limits, exercised over real HTTP routes.

Covers the four production-readiness fixes:
  - security response headers (nosniff / referrer-policy / frame-options)
  - /api/health returns 503 (not 200) when the DB is unreachable
  - the refresh cookie carries hardened attributes + the Secure flag honors config
  - the dataset runtime endpoints are rate-limited (429)
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
_DB = _TMP / "test_transport.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_transport")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "transport-test")

from src import config  # noqa: E402
from src.database import init_db  # noqa: E402
from src.main import app  # noqa: E402


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


# --- security headers ------------------------------------------------------
def test_security_headers_present(client):
    r = client.get("/api/health")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
    # No CSP frame-ancestors on this route → we add SAMEORIGIN framing protection.
    assert r.headers.get("x-frame-options") == "SAMEORIGIN"
    # HSTS only on HTTPS; TestClient is http, so it must be absent.
    assert "strict-transport-security" not in {k.lower() for k in r.headers}


# --- health status code ----------------------------------------------------
def test_health_ok_returns_200(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"
    assert r.json()["database"] == "ok"


def test_health_returns_503_when_db_down(client, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("db unreachable")

    # The health handler grabs async_session from src.database at call time.
    monkeypatch.setattr("src.database.async_session", _boom)
    r = client.get("/api/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["database"] == "error"


# --- refresh cookie hardening ----------------------------------------------
def test_login_sets_hardened_refresh_cookie(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "password"})
    assert r.status_code == 200
    sc = r.headers.get("set-cookie", "").lower()
    assert "refresh_token=" in sc
    assert "httponly" in sc
    assert "samesite=strict" in sc
    assert "path=/api/auth" in sc
    # Auto mode over plain HTTP (TestClient) must NOT mark the cookie Secure.
    assert "secure" not in sc


def test_cookie_secure_can_be_forced(client, monkeypatch):
    monkeypatch.setattr(config.settings, "cookie_secure", True)
    r = client.post("/api/auth/login", json={"username": "admin", "password": "password"})
    assert r.status_code == 200
    assert "secure" in r.headers.get("set-cookie", "").lower()


# --- dataset runtime rate limiting -----------------------------------------
def test_dataset_execute_is_rate_limited(client, admin_token):
    from src.rate_limit import dataset_limiter

    orig_cap, orig_rate = dataset_limiter.capacity, dataset_limiter.rate
    dataset_limiter.reset()
    dataset_limiter.capacity = 1      # one token, no refill → 2nd call trips
    dataset_limiter.rate = 0.0
    try:
        h = _auth(admin_token)
        url = "/api/apps/nope/datasets/nope/execute"
        first = client.post(url, json={"params": {}}, headers=h)
        # First call passes the limiter and fails downstream (no binding) — not 429.
        assert first.status_code != 429, first.text
        second = client.post(url, json={"params": {}}, headers=h)
        assert second.status_code == 429
        assert "rate limit" in second.json()["detail"].lower()
    finally:
        dataset_limiter.capacity = orig_cap
        dataset_limiter.rate = orig_rate
        dataset_limiter.reset()
