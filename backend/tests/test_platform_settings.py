"""Tests for platform settings + LLM budget enforcement."""
from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_platform_settings.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_settings")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "settings-test")

from src.database import async_session, init_db  # noqa: E402
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
    r = client.post("/api/auth/login", json={"username": "admin", "password": "password"})
    return r.json()["access_token"]


def _auth(t): return {"Authorization": f"Bearer {t}"}


def test_settings_get_defaults(client, admin_token):
    r = client.get("/api/admin/settings", headers=_auth(admin_token))
    assert r.status_code == 200
    body = r.json()
    assert "custom_system_prompt" in body
    assert "monthly_budget_usd" in body
    assert body["budget_alert_threshold"] == 0.8


def test_settings_update_and_persist(client, admin_token):
    r = client.put("/api/admin/settings", json={
        "custom_system_prompt": "Always use teal accents and rounded-2xl cards.",
        "monthly_budget_usd": 100.0,
        "per_user_budget_usd": 25.0,
    }, headers=_auth(admin_token))
    assert r.status_code == 200
    body = r.json()
    assert body["custom_system_prompt"].startswith("Always use teal")
    assert body["monthly_budget_usd"] == 100.0

    # Re-fetch to confirm persistence
    r = client.get("/api/admin/settings", headers=_auth(admin_token))
    assert r.json()["per_user_budget_usd"] == 25.0


def test_non_admin_cannot_read_settings(client):
    r = client.post("/api/auth/login", json={"username": "developer", "password": "password"})
    dev = r.json()["access_token"]
    r = client.get("/api/admin/settings", headers=_auth(dev))
    assert r.status_code in (401, 403)


def test_budget_status_endpoint(client, admin_token):
    r = client.get("/api/admin/settings/budget-status", headers=_auth(admin_token))
    assert r.status_code == 200
    body = r.json()
    assert "allowed" in body
    assert "user_spent" in body


def test_budget_blocks_when_user_over_cap():
    """Directly test check_budget: set a $1 per-user cap, record $2 of spend,
    confirm it blocks."""
    from src.platform_settings.service import check_budget, set_setting
    from src.llm_usage.service import record_usage

    async def _run():
        async with async_session() as db:
            # Find the admin user id
            from src.auth.models import User
            from sqlalchemy import select
            admin = (await db.execute(select(User).where(User.username == "admin"))).scalar_one()

            await set_setting(db, "per_user_budget_usd", 0.01)  # tiny cap

            # Record some spend that exceeds it (gpt-4o pricing on big tokens)
            await record_usage(db, user_id=admin.id, app_id="a1",
                               provider_type="openai", model="gpt-4o",
                               purpose="generation",
                               input_tokens=2_000_000, output_tokens=2_000_000)

            status = await check_budget(db, admin.id)
            assert status.allowed is False
            assert "budget" in status.reason.lower()

            # Reset cap so other tests aren't affected
            await set_setting(db, "per_user_budget_usd", 0.0)

    asyncio.run(_run())


def test_unlimited_budget_always_allows():
    from src.platform_settings.service import check_budget, set_setting

    async def _run():
        async with async_session() as db:
            from src.auth.models import User
            from sqlalchemy import select
            admin = (await db.execute(select(User).where(User.username == "admin"))).scalar_one()
            await set_setting(db, "per_user_budget_usd", 0.0)  # unlimited
            await set_setting(db, "monthly_budget_usd", 0.0)
            status = await check_budget(db, admin.id)
            assert status.allowed is True

    asyncio.run(_run())
