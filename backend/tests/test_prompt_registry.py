"""Admin AI prompt registry — visibility, override (that genuinely takes effect),
reset, flow model, authz, and audit. Hits the real routes via TestClient.
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
_DB = _TMP / "test_prompt_registry.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_prompt_registry")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "prompt-registry-test")

from src.database import async_session, init_db  # noqa: E402
from src.main import app  # noqa: E402
from src.ai_prompts import registry  # noqa: E402


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


@pytest.fixture(scope="module")
def user_token(client):
    return client.post("/api/auth/login", json={"username": "user", "password": "password"}).json()["access_token"]


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


def test_flow_is_ordered_and_attaches_prompts():
    stages = registry.flow()
    assert [s["id"] for s in stages][:4] == ["context", "generate", "verify", "self_heal"]
    gen = next(s for s in stages if s["id"] == "generate")
    assert "system_prompt" in {p["key"] for p in gen["prompts"]}
    ctx = next(s for s in stages if s["id"] == "context")
    assert any(p["key"] == "no_datasets_notice" for p in ctx["prompts"])


def test_list_prompts_returns_defaults(client, admin_token):
    r = client.get("/api/admin/ai/prompts", headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    prompts = {p["key"]: p for p in r.json()["prompts"]}
    assert "system_prompt" in prompts
    assert prompts["system_prompt"]["default"].strip() != ""
    assert prompts["system_prompt"]["is_overridden"] is False
    assert prompts["system_prompt"]["effective"] == prompts["system_prompt"]["default"]


def test_flow_endpoint(client, admin_token):
    r = client.get("/api/admin/ai/flow", headers=_auth(admin_token))
    assert r.status_code == 200
    assert len(r.json()["stages"]) >= 4


def test_override_takes_effect_and_resets(client, admin_token):
    r = client.put("/api/admin/ai/prompts/system_prompt",
                   json={"text": "OVERRIDDEN SYSTEM PROMPT XYZ"}, headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    assert r.json()["prompt"]["is_overridden"] is True

    prompts = {p["key"]: p for p in
               client.get("/api/admin/ai/prompts", headers=_auth(admin_token)).json()["prompts"]}
    assert prompts["system_prompt"]["effective"] == "OVERRIDDEN SYSTEM PROMPT XYZ"

    # The function the generator actually calls must return the override (no dead setting).
    async def _resolve():
        async with async_session() as db:
            return await registry.resolve(db, "system_prompt")
    assert asyncio.run(_resolve()) == "OVERRIDDEN SYSTEM PROMPT XYZ"

    r = client.post("/api/admin/ai/prompts/system_prompt/reset", headers=_auth(admin_token))
    assert r.status_code == 200
    assert r.json()["prompt"]["is_overridden"] is False
    assert asyncio.run(_resolve()) != "OVERRIDDEN SYSTEM PROMPT XYZ"


def test_non_admin_forbidden(client, user_token):
    assert client.get("/api/admin/ai/prompts", headers=_auth(user_token)).status_code == 403


def test_unknown_key_404(client, admin_token):
    assert client.put("/api/admin/ai/prompts/nope", json={"text": "x"},
                      headers=_auth(admin_token)).status_code == 404


def test_override_is_audited(client, admin_token):
    client.put("/api/admin/ai/prompts/no_datasets_notice",
               json={"text": "custom no-data"}, headers=_auth(admin_token))

    async def _count():
        from sqlalchemy import select, func
        from src.secrets.models import AuditLog
        async with async_session() as db:
            return (await db.execute(
                select(func.count()).select_from(AuditLog).where(AuditLog.action == "ai_prompt.override")
            )).scalar_one()
    assert asyncio.run(_count()) >= 1
    client.post("/api/admin/ai/prompts/no_datasets_notice/reset", headers=_auth(admin_token))
