"""Generation traceability (#4): the trace store + read API, AND proof that the
real chat() generator actually records a trace (not a placeholder).
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_generation_trace.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_generation_trace")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "generation-trace-test")

from src.config import settings  # noqa: E402
from src.database import async_session, init_db  # noqa: E402
from src.main import app  # noqa: E402
from src.ai import service as ai_service_mod  # noqa: E402
from src.ai_providers.service import ai_provider_service  # noqa: E402
from src.generation_trace.service import TraceBuilder, list_traces, get_trace  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _init():
    asyncio.run(init_db())
    yield


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _token(client, who):
    return client.post("/api/auth/login", json={"username": who, "password": "password"}).json()["access_token"]


@pytest.fixture(scope="module")
def admin_token(client):
    return _token(client, "admin")


@pytest.fixture(scope="module")
def dev_token(client):
    return _token(client, "developer")


@pytest.fixture(scope="module")
def user_token(client):
    return _token(client, "user")


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


def _admin_id() -> str:
    conn = sqlite3.connect(settings.database_url[len("sqlite+aiosqlite:///"):])
    try:
        return conn.execute("SELECT id FROM users WHERE username='admin' LIMIT 1").fetchone()[0]
    finally:
        conn.close()


def _insert_app(app_id: str, created_by: str, verify="off"):
    conn = sqlite3.connect(settings.database_url[len("sqlite+aiosqlite:///"):])
    try:
        conn.execute(
            "INSERT OR IGNORE INTO apps (id, name, description, icon, status, current_version, "
            "ai_toggle_enabled, bug_widget_enabled, bug_fix_auto_approve_max_risk, "
            "ai_verify_level, ai_verify_max_iterations, created_by, created_at, updated_at) "
            f"VALUES (?, ?, '', 'app-window', 'draft', 0, 0, 0, 'none', '{verify}', 0, ?, "
            "datetime('now'), datetime('now'))",
            (app_id, f"trace-{app_id[:8]}", created_by),
        )
        conn.commit()
    finally:
        conn.close()


# --- a tiny fake streaming LLM response ------------------------------------
class _Delta:
    def __init__(self, content): self.content = content


class _Choice:
    def __init__(self, content): self.delta = _Delta(content)


class _Usage:
    def __init__(self, i, o): self.prompt_tokens = i; self.completion_tokens = o


class _Chunk:
    def __init__(self, content, usage=None):
        self.choices = [_Choice(content)] if content is not None else []
        self.usage = usage


async def _fake_stream(*chunks):
    for c in chunks:
        yield c


def test_tracebuilder_save_and_read_api(client, admin_token, dev_token, user_token):
    app_id = str(uuid.uuid4())
    _insert_app(app_id, _admin_id())

    async def _seed():
        async with async_session() as db:
            tb = TraceBuilder(app_id=app_id, user_id="u1", user_message="build a dashboard",
                              system_prompts=["SYS A", "SYS B"], model="gpt-4o", provider="openai")
            tb.step(type="context", system_prompt_count=2)
            tb.step(type="generate", files=["src/App.tsx"])
            tb.finalize("passed", summary="ok")
            return await tb.save(db)
    tid = asyncio.run(_seed())

    r = client.get(f"/api/apps/{app_id}/traces", headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    assert any(t["id"] == tid for t in r.json()["traces"])

    detail = client.get(f"/api/apps/{app_id}/traces/{tid}", headers=_auth(admin_token)).json()
    assert detail["user_message"] == "build a dashboard"
    assert detail["system_prompts"] == ["SYS A", "SYS B"]
    assert any(s["type"] == "generate" for s in detail["steps"])

    # developers can read; plain users cannot
    assert client.get(f"/api/apps/{app_id}/traces", headers=_auth(dev_token)).status_code == 200
    assert client.get(f"/api/apps/{app_id}/traces", headers=_auth(user_token)).status_code == 403
    # unknown trace → 404
    assert client.get(f"/api/apps/{app_id}/traces/nope", headers=_auth(admin_token)).status_code == 404


def test_chat_records_a_trace(client, admin_token, monkeypatch):
    """The REAL chat() generator must persist a trace with the system prompts + steps."""
    app_id = str(uuid.uuid4())
    admin_id = _admin_id()
    _insert_app(app_id, admin_id, verify="off")

    cfg = {"provider_type": "openai", "model": "gpt-4o-mini", "api_key": "x", "base_url": None}

    async def fake_get_default(db, purpose="generation"):
        return cfg
    monkeypatch.setattr(ai_provider_service, "get_default_provider_config", fake_get_default)

    async def fake_acompletion(**kwargs):
        return _fake_stream(_Chunk("Here is a plain-text answer with no code."),
                            _Chunk(None, usage=_Usage(10, 20)))
    monkeypatch.setattr(ai_service_mod, "acompletion", fake_acompletion)

    async def _run():
        types = []
        async with async_session() as db:
            async for ev in ai_service_mod.ai_service.chat(db, app_id, "build me a thing", user_id=admin_id):
                types.append(ev["type"])
        return types
    types = asyncio.run(_run())
    assert "done" in types

    async def _detail():
        async with async_session() as db:
            traces = await list_traces(db, app_id)
            assert traces, "chat() did not record a trace"
            return await get_trace(db, traces[0]["id"])
    detail = asyncio.run(_detail())
    assert detail["user_message"] == "build me a thing"
    assert len(detail["system_prompts"]) >= 1          # SYSTEM_PROMPT + no-datasets notice
    step_types = [s["type"] for s in detail["steps"]]
    assert "context" in step_types and "generate" in step_types
    assert detail["status"] in ("no_files", "no_verify", "passed", "failed")
