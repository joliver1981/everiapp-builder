"""Per-provider + admin-tunable LLM timeouts (v0.19.0).

Field case: a deep-reasoning model (Fable 5) sat quiet past the old global
180s stream-silence cap and every builder turn aborted with "No response
data ... for 180s", while a faster-to-first-token model on the same install
worked — the operator had no way to raise the limit without editing the
service .env and restarting. Now: per-provider timeout (Admin → AI
Providers, stored in the provider secret's metadata) > admin platform
settings (no restart) > .env defaults.

HTTP round-trips run through the REAL routes (TestClient, real login).
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
_DB = _TMP / "test_llm_timeouts.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_llm_timeouts")
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "llm-timeouts-test")

from src.database import init_db  # noqa: E402
from src.main import app as fastapi_app  # noqa: E402


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


# ------------------------------------------------- provider field round-trip

def test_provider_timeout_roundtrips_and_clears(client, admin):
    p = client.post("/api/admin/ai-providers", json={
        "name": "Slow Reasoner", "provider_type": "anthropic", "api_key": "k",
        "default_model": "claude-fable-5", "timeout_seconds": 900,
    }, headers=admin).json()
    try:
        assert p["timeout_seconds"] == 900

        got = client.get(f"/api/admin/ai-providers/{p['id']}", headers=admin).json()
        assert got["timeout_seconds"] == 900

        # PUT a new value; then clear back to inherit with 0.
        r = client.put(f"/api/admin/ai-providers/{p['id']}",
                       json={"timeout_seconds": 1200}, headers=admin)
        assert r.status_code == 200, r.text
        assert r.json()["timeout_seconds"] == 1200

        r = client.put(f"/api/admin/ai-providers/{p['id']}",
                       json={"timeout_seconds": 0}, headers=admin)
        assert r.json()["timeout_seconds"] == 0

        # A PUT that doesn't mention the field must not clobber it (lossy
        # round-trip guard — the admin form sends explicit fields only).
        client.put(f"/api/admin/ai-providers/{p['id']}",
                   json={"timeout_seconds": 777}, headers=admin)
        r = client.put(f"/api/admin/ai-providers/{p['id']}",
                       json={"name": "Slow Reasoner Renamed"}, headers=admin)
        assert r.json()["timeout_seconds"] == 777
    finally:
        client.delete(f"/api/admin/ai-providers/{p['id']}", headers=admin)


def test_provider_timeout_rejects_out_of_range(client, admin):
    r = client.post("/api/admin/ai-providers", json={
        "name": "Bad Timeout", "provider_type": "openai", "api_key": "k",
        "default_model": "gpt-5.5", "timeout_seconds": 900000,
    }, headers=admin)
    assert r.status_code == 422


# ------------------------------------------------- platform settings + resolution

def test_platform_timeout_settings_roundtrip(client, admin):
    try:
        r = client.put("/api/admin/settings", json={
            "llm_stream_timeout_seconds": 900, "llm_request_timeout_seconds": 1200,
        }, headers=admin)
        assert r.status_code == 200, r.text
        got = client.get("/api/admin/settings", headers=admin).json()
        assert got["llm_stream_timeout_seconds"] == 900
        assert got["llm_request_timeout_seconds"] == 1200
    finally:
        client.put("/api/admin/settings", json={
            "llm_stream_timeout_seconds": 0, "llm_request_timeout_seconds": 0,
        }, headers=admin)


def test_effective_timeouts_resolution_chain(client, admin):
    """provider override > admin setting > .env default — each tier verified."""
    from src.config import settings as cfg
    from src.database import async_session
    from src.platform_settings.service import effective_llm_timeouts, set_setting

    async def resolve(provider_config=None):
        async with async_session() as db:
            return await effective_llm_timeouts(db, provider_config)

    async def put_setting(key, value):
        async with async_session() as db:
            await set_setting(db, key, value)

    # Tier 3: nothing set anywhere → .env / coded defaults.
    stream, request = asyncio.run(resolve())
    assert stream == cfg.llm_stream_timeout
    assert request == cfg.llm_request_timeout

    try:
        # Tier 2: admin settings override the .env defaults, no restart.
        asyncio.run(put_setting("llm_stream_timeout_seconds", 901))
        asyncio.run(put_setting("llm_request_timeout_seconds", 1201))
        assert asyncio.run(resolve()) == (901, 1201)

        # Tier 1: the provider's own timeout beats both, for both values.
        assert asyncio.run(resolve({"timeout_seconds": 2000})) == (2000, 2000)

        # 0 / garbage on the provider falls through to the admin tier.
        assert asyncio.run(resolve({"timeout_seconds": 0})) == (901, 1201)
        assert asyncio.run(resolve({"timeout_seconds": "nonsense"})) == (901, 1201)

        # Explicit values are floor/ceiling-clamped, never rejected.
        assert asyncio.run(resolve({"timeout_seconds": 1})) == (10, 10)
    finally:
        asyncio.run(put_setting("llm_stream_timeout_seconds", 0))
        asyncio.run(put_setting("llm_request_timeout_seconds", 0))

    # Restored: back to the .env defaults.
    assert asyncio.run(resolve()) == (cfg.llm_stream_timeout, cfg.llm_request_timeout)
