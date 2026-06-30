"""#5 — the AI error feedback loop (self-heal) end-to-end.

Drives the real `_self_heal_loop` with a mocked verifier + LLM to prove:
  - a failing verify feeds errors back to the LLM, the fix is applied, and a
    passing re-verify ends the loop green;
  - a fix that doesn't change the errors stops early (no 8x churn);
  - a data/config failure is surfaced with actionable guidance.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import uuid
from pathlib import Path

import pytest

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_self_heal_loop.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_self_heal")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "self-heal-test")

from src.config import settings  # noqa: E402
from src.database import async_session, init_db  # noqa: E402
from src.ai import service as svc  # noqa: E402
from src.ai.code_parser import GeneratedFile  # noqa: E402
from src.ai.verifier import VerifyError, VerifyResult  # noqa: E402
from src.apps.models import Conversation  # noqa: E402

CFG = {"provider_type": "openai", "model": "gpt-4o", "api_key": "x", "base_url": None}


@pytest.fixture(scope="module", autouse=True)
def _init():
    async def _setup():
        await init_db()
        from sqlalchemy import select
        from src.auth.models import User
        async with async_session() as db:
            exists = (await db.execute(
                select(User).where(User.username == "admin")
            )).scalar_one_or_none()
            if not exists:
                # FK enforcement is ON, so apps.created_by must reference a real
                # user. Seed the admin DIRECTLY — do NOT start the app lifespan via
                # TestClient just to seed: that start/stop cycle deadlocks under
                # full-suite state. Mock-AD users have no password field.
                db.add(User(username="admin", display_name="Admin", role="admin"))
                await db.commit()
    asyncio.run(_setup())
    yield


def _err(msg, stage="tsc"):
    return VerifyError(stage=stage, file=None, line=None, column=None, code="TS2304", message=msg)


def _insert_app(app_id):
    conn = sqlite3.connect(settings.database_url[len("sqlite+aiosqlite:///"):])
    try:
        row = conn.execute("SELECT id FROM users WHERE username='admin' LIMIT 1").fetchone()
        conn.execute(
            "INSERT OR IGNORE INTO apps (id, name, description, icon, status, current_version, "
            "ai_toggle_enabled, bug_widget_enabled, bug_fix_auto_approve_max_risk, "
            "ai_verify_level, ai_verify_max_iterations, created_by, created_at, updated_at) "
            "VALUES (?, ?, '', 'app-window', 'draft', 0, 0, 0, 'none', 'tsc_build_boot', 8, ?, "
            "datetime('now'), datetime('now'))",
            (app_id, f"heal-{app_id[:8]}", row[0]),
        )
        conn.commit()
    finally:
        conn.close()


class _FixResp:
    class _C:
        class _M:
            content = "// FILE: src/App.tsx\nfixed"
        message = _M()
    choices = [_C()]


def _wire(monkeypatch, verify_results):
    """verify_results: list of VerifyResult returned by successive verify_app calls
    (last one repeats once exhausted)."""
    state = {"i": 0}

    async def fake_verify(app_id, level, runtime_enabled=True):
        i = min(state["i"], len(verify_results) - 1)
        state["i"] += 1
        return verify_results[i]

    async def fake_acompletion(**kw):
        return _FixResp()

    def fake_parse(raw):
        return ([GeneratedFile(path="src/App.tsx", content="x", action="create")], "desc", None)

    async def fake_save(self, app_id, files):
        return None

    monkeypatch.setattr(svc, "verify_app", fake_verify)
    monkeypatch.setattr(svc, "acompletion", fake_acompletion)
    monkeypatch.setattr(svc, "parse_llm_response", fake_parse)
    monkeypatch.setattr(svc.AIService, "_save_generated_files", fake_save)


async def _run_loop(app_id):
    async with async_session() as db:
        conv = Conversation(app_id=app_id, title="t")
        db.add(conv)
        await db.flush()
        events = []
        async for ev in svc.ai_service._self_heal_loop(
            db, conv, app_id, "original response", "tsc_build_boot", 8, CFG
        ):
            events.append(ev)
        return events


def _final(events):
    return [e["data"] for e in events if e["type"] == "_final_verify"][0]


def test_feedback_loop_fixes_then_passes(monkeypatch):
    app_id = str(uuid.uuid4())
    _insert_app(app_id)
    _wire(monkeypatch, [
        VerifyResult(stage_reached="tsc", errors=[_err("Cannot find name 'x'")], summary="1 error"),
        VerifyResult(stage_reached="done", errors=[], summary="ok"),
    ])
    events = asyncio.run(_run_loop(app_id))
    types = [e["type"] for e in events]
    assert types.count("files") == 1                 # exactly one fix applied
    assert _final(events).passed is True             # re-verify is green
    iters = [e["data"] for e in events if e["type"] == "verify_iteration"]
    assert iters[0]["passed"] is False and iters[-1]["passed"] is True


def test_feedback_loop_stops_on_no_progress(monkeypatch):
    app_id = str(uuid.uuid4())
    _insert_app(app_id)
    # Same error every verify → a fix that changes nothing → stop after one attempt.
    _wire(monkeypatch, [
        VerifyResult(stage_reached="tsc", errors=[_err("Cannot find name 'x'")], summary="1 error"),
    ])
    events = asyncio.run(_run_loop(app_id))
    types = [e["type"] for e in events]
    assert types.count("files") == 1                 # did NOT churn all 8 iterations
    assert _final(events).passed is False
    assert any(e.get("data", {}).get("stage") == "stopped"
               for e in events if e["type"] == "verify_iteration")


def test_feedback_loop_surfaces_config_issue(monkeypatch):
    app_id = str(uuid.uuid4())
    _insert_app(app_id)
    _wire(monkeypatch, [
        VerifyResult(stage_reached="runtime",
                     errors=[_err("useDataset('sales') failed: dataset not found", stage="runtime")],
                     summary="runtime error"),
    ])
    events = asyncio.run(_run_loop(app_id))
    final = _final(events)
    assert final.passed is False
    assert final.errors[0].stage == "config"          # actionable guidance prepended
    assert "Datasets" in final.errors[0].message


async def _run_loop_live(app_id):
    async with async_session() as db:
        conv = Conversation(app_id=app_id, title="t")
        db.add(conv)
        await db.flush()
        events = []
        async for ev in svc.ai_service._self_heal_loop(
            db, conv, app_id, "original response", "tsc_build_boot", 8, CFG, live_code=True
        ):
            events.append(ev)
        return events


def test_live_code_replays_fixed_files(monkeypatch):
    app_id = str(uuid.uuid4())
    _insert_app(app_id)
    _wire(monkeypatch, [
        VerifyResult(stage_reached="tsc", errors=[_err("Cannot find name 'x'")], summary="1 error"),
        VerifyResult(stage_reached="done", errors=[], summary="ok"),
    ])
    events = asyncio.run(_run_loop_live(app_id))
    cs = [e["data"] for e in events if e["type"] == "code_stream"]
    assert any(e["event"] == "file_start" and e["path"] == "src/App.tsx" for e in cs)
    assert any(e["event"] == "file_end" for e in cs)


def test_no_code_stream_without_live_flag(monkeypatch):
    app_id = str(uuid.uuid4())
    _insert_app(app_id)
    _wire(monkeypatch, [
        VerifyResult(stage_reached="tsc", errors=[_err("Cannot find name 'x'")], summary="1 error"),
        VerifyResult(stage_reached="done", errors=[], summary="ok"),
    ])
    events = asyncio.run(_run_loop(app_id))      # positional call, live_code defaults False
    assert not any(e["type"] == "code_stream" for e in events)
