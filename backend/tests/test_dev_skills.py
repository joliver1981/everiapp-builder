"""Developer skills: personal standing preferences injected into generation.

- GET/PUT /api/auth/me/dev-standards round-trip (40k cap).
- The REAL ai_service.chat() injects the developer's personal skills and the
  org-wide custom_system_prompt as system messages (fake streaming LLM
  captures what the model actually receives).
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_dev_skills.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_dev_skills")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "dev-skills-test")

from src.database import async_session, init_db  # noqa: E402
from src.main import app as fastapi_app  # noqa: E402
from src.ai import service as ai_service_mod  # noqa: E402
from src.ai_providers.service import ai_provider_service  # noqa: E402

SKILL = "When using SQLite, always enable WAL for concurrent writes."


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


def test_dev_standards_roundtrip(client, admin):
    assert client.get("/api/auth/me/dev-standards", headers=admin).json() == {"dev_standards": ""}
    r = client.put("/api/auth/me/dev-standards", json={"dev_standards": SKILL}, headers=admin)
    assert r.status_code == 200 and r.json()["dev_standards"] == SKILL
    assert client.get("/api/auth/me/dev-standards", headers=admin).json()["dev_standards"] == SKILL
    # A rich standards doc that was silently cut at the old 8k now fits.
    big = "x" * 30000
    r = client.put("/api/auth/me/dev-standards", json={"dev_standards": big}, headers=admin)
    assert r.json()["dev_standards"] == big and len(r.json()["dev_standards"]) == 30000
    # Cap: still truncates (not rejects) beyond the generous 40k ceiling.
    r = client.put("/api/auth/me/dev-standards", json={"dev_standards": "x" * 41000}, headers=admin)
    assert len(r.json()["dev_standards"]) == 40000
    client.put("/api/auth/me/dev-standards", json={"dev_standards": SKILL}, headers=admin)
    # Anonymous: no.
    assert client.get("/api/auth/me/dev-standards").status_code in (401, 403)


def test_skills_injected_into_generation(client, admin, monkeypatch):
    captured: list[list[dict]] = []

    class _Chunk:
        def __init__(self, content):
            self.choices = [type("C", (), {"delta": type("D", (), {"content": content})()})()]
            self.usage = None

    async def fake_stream():
        yield _Chunk("Done — nothing to change.")

    async def fake_acompletion(**kwargs):
        captured.append(kwargs["messages"])
        return fake_stream()
    monkeypatch.setattr(ai_service_mod, "acompletion", fake_acompletion)

    async def fake_provider(db, purpose="generation"):
        return {"provider_type": "openai", "model": "m", "api_key": "k", "base_url": None}
    monkeypatch.setattr(ai_provider_service, "get_default_provider_config", fake_provider)

    # Org-wide standard + this user's personal skill.
    from src.platform_settings.service import set_setting

    async def _org():
        async with async_session() as db:
            await set_setting(db, "custom_system_prompt", "All apps use the corporate blue theme.")
    asyncio.run(_org())

    me = client.get("/api/auth/me", headers=admin).json()["user"]
    app_id = client.post("/api/apps", json={"name": "Skills App"}, headers=admin).json()["id"]

    async def _chat():
        async with async_session() as db:
            async for _ev in ai_service_mod.ai_service.chat(
                db, app_id, "build me a todo app", user_id=me["id"],
            ):
                pass
    asyncio.run(_chat())

    system_text = "\n".join(m["content"] for m in captured[0] if m["role"] == "system")
    assert SKILL in system_text                              # personal skill
    assert "corporate blue theme" in system_text             # org standard
    assert "developer's standing preferences" in system_text  # labeled block


def test_history_keeps_all_user_messages_plus_recent_window():
    """Long builds must not forget early instructions: EVERY user message is
    replayed, plus the most-recent `window` messages of full context. Older
    (large, code-carrying) assistant turns beyond the window are dropped."""
    from types import SimpleNamespace
    from src.ai.service import _select_history_messages

    msgs = [
        SimpleNamespace(role="user", content="u1"),
        SimpleNamespace(role="assistant", content="a1"),
        SimpleNamespace(role="user", content="u2"),
        SimpleNamespace(role="assistant", content="a2"),
        SimpleNamespace(role="user", content="u3"),
        SimpleNamespace(role="assistant", content="a3"),
    ]
    contents = [m.content for m in _select_history_messages(msgs, window=2)]
    # window=2 → tail {u3, a3}; plus all earlier user messages u1, u2.
    assert contents == ["u1", "u2", "u3", "a3"]
    assert "a1" not in contents and "a2" not in contents      # stale assistant code dropped
    assert len(contents) == len(set(contents))                 # chronological, no dupes
