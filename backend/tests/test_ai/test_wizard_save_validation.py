"""AI-generated wizards must pass validate_wizard before being stored.

The chat pipeline used to save whatever parse_llm_response called a wizard —
so a schema with duplicate keys (or, pre-hardening, a {"steps": ["..."]}
junk reply) landed in apps.setup_wizard, 500'd GET /setup-status and
POST /setup, and blocked every subsequent manual save (PUT re-validates the
whole document). These run the REAL ai_service.chat() generator against a
fake streaming LLM (same harness as test_code_stream.py) and check what
actually lands in the database.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_wizard_save_validation.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_wizard_save")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "wizard-save-test")

from src.config import settings  # noqa: E402
from src.database import async_session, init_db  # noqa: E402
from src.main import app  # noqa: E402
from src.ai import service as ai_service_mod  # noqa: E402
from src.ai_providers.service import ai_provider_service  # noqa: E402

VALID_WIZARD = {
    "title": "App Setup",
    "steps": [{"title": "Keys", "fields": [
        {"key": "api_key", "label": "API Key", "type": "secret", "required": True},
    ]}],
}
# Wizard-shaped (dict steps, dict fields) but schema-invalid: duplicate keys.
INVALID_WIZARD = {
    "title": "App Setup",
    "steps": [{"title": "Keys", "fields": [{"key": "dup"}, {"key": "dup"}]}],
}


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
    # Mock-AD (DEBUG) creates the admin user row on first login — required
    # before _admin_id() can look it up.
    r = client.post("/api/auth/login", json={"username": "admin", "password": "password"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _sqlite():
    return sqlite3.connect(settings.database_url[len("sqlite+aiosqlite:///"):])


def _admin_id() -> str:
    conn = _sqlite()
    try:
        return conn.execute("SELECT id FROM users WHERE username='admin' LIMIT 1").fetchone()[0]
    finally:
        conn.close()


def _insert_app(app_id: str, created_by: str):
    conn = _sqlite()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO apps (id, name, description, icon, status, current_version, "
            "ai_toggle_enabled, bug_widget_enabled, bug_fix_auto_approve_max_risk, "
            "ai_verify_level, ai_verify_max_iterations, created_by, created_at, updated_at) "
            "VALUES (?, ?, '', 'app-window', 'draft', 0, 0, 0, 'none', 'off', 0, ?, "
            "datetime('now'), datetime('now'))",
            (app_id, f"wsv-{app_id[:8]}", created_by),
        )
        conn.commit()
    finally:
        conn.close()


def _stored_wizard(app_id: str):
    conn = _sqlite()
    try:
        row = conn.execute("SELECT setup_wizard FROM apps WHERE id=?", (app_id,)).fetchone()
    finally:
        conn.close()
    return json.loads(row[0]) if row and row[0] else None


# --- fake streaming LLM (same shapes as test_code_stream) -------------------
class _Delta:
    def __init__(self, content): self.content = content


class _Choice:
    def __init__(self, content): self.delta = _Delta(content)


class _Chunk:
    def __init__(self, content):
        self.choices = [_Choice(content)] if content is not None else []
        self.usage = None


async def _fake_stream(*chunks):
    for c in chunks:
        yield c


def _wire(monkeypatch, reply: str):
    cfg = {"provider_type": "openai", "model": "gpt-4o-mini", "api_key": "x", "base_url": None}

    async def fake_get_default(db, purpose="generation"):
        return cfg
    monkeypatch.setattr(ai_provider_service, "get_default_provider_config", fake_get_default)

    async def fake_acompletion(**kwargs):
        return _fake_stream(_Chunk(reply))
    monkeypatch.setattr(ai_service_mod, "acompletion", fake_acompletion)


def _chat(app_id: str, user_id: str) -> list[dict]:
    async def _run():
        events = []
        async with async_session() as db:
            async for ev in ai_service_mod.ai_service.chat(
                db, app_id, "create a setup wizard", user_id=user_id,
            ):
                events.append(ev)
        return events
    return asyncio.run(_run())


def test_valid_ai_wizard_is_saved(client, admin_token, monkeypatch):
    app_id = str(uuid.uuid4())
    _insert_app(app_id, _admin_id())
    _wire(monkeypatch, json.dumps(VALID_WIZARD))

    events = _chat(app_id, _admin_id())

    assert any(e["type"] == "wizard" for e in events)
    assert not any(e["type"] == "wizard_invalid" for e in events)
    assert _stored_wizard(app_id) == VALID_WIZARD


def test_invalid_ai_wizard_is_discarded(client, admin_token, monkeypatch):
    app_id = str(uuid.uuid4())
    _insert_app(app_id, _admin_id())
    _wire(monkeypatch, json.dumps(INVALID_WIZARD))

    events = _chat(app_id, _admin_id())

    invalid = [e for e in events if e["type"] == "wizard_invalid"]
    assert invalid and any("duplicate" in err for err in invalid[0]["data"]["errors"])
    assert not any(e["type"] == "wizard" for e in events)
    assert _stored_wizard(app_id) is None


def test_invalid_ai_wizard_never_blocks_manual_editor(client, admin_token, monkeypatch):
    """The lockout regression: after a bad AI wizard turn, PUT /wizard still works."""
    app_id = str(uuid.uuid4())
    _insert_app(app_id, _admin_id())
    _wire(monkeypatch, json.dumps(INVALID_WIZARD))
    _chat(app_id, _admin_id())

    r = client.put(f"/api/apps/{app_id}/wizard", json=VALID_WIZARD,
                   headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200, r.text


def test_steps_of_strings_reply_stays_conversational(client, admin_token, monkeypatch):
    """A junk {"steps": [str, ...]} reply must not touch the stored wizard at all."""
    app_id = str(uuid.uuid4())
    _insert_app(app_id, _admin_id())
    _wire(monkeypatch, json.dumps({"steps": ["clone the repo", "run npm install"]}))

    events = _chat(app_id, _admin_id())

    assert not any(e["type"] in ("wizard", "wizard_invalid") for e in events)
    assert any(e["type"] == "done" for e in events)
    assert _stored_wizard(app_id) is None
