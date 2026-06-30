"""Live code-streaming + jump-refs through the REAL chat() generator and the REAL
/api/ai/chat websocket route.

Mirrors test_generation_trace.py's harness (raw-sqlite app seed + provider mock +
fake streaming acompletion). Proves:
  - live_code=True emits code_stream file_start/delta/file_end for each generated file;
  - live_code=False emits NO code_stream, but `done.code_refs` is populated either way;
  - prose `text` is byte-identical whether or not the user is watching live;
  - the websocket route actually reads `live_code` off the payload.
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
_DB = _TMP / "test_code_stream.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_code_stream")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "code-stream-test")

from src.config import settings  # noqa: E402
from src.database import async_session, init_db  # noqa: E402
from src.main import app  # noqa: E402
from src.ai import service as ai_service_mod  # noqa: E402
from src.ai_providers.service import ai_provider_service  # noqa: E402


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
            (app_id, f"cs-{app_id[:8]}", created_by),
        )
        conn.commit()
    finally:
        conn.close()


# --- fake streaming LLM (same shapes as test_generation_trace) -------------
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


# A response with one file block (split across chunks) + a trailing jump directive.
_CHUNKS = [
    "I'll build it. ",
    "```tsx\n// FILE: src/App.tsx\n",
    "export default function App() {\n",
    "  return <div>hi</div>\n}\n```\n",
    "Done — see [[jump:src/App.tsx:1-3]].",
]


def _wire(monkeypatch):
    cfg = {"provider_type": "openai", "model": "gpt-4o-mini", "api_key": "x", "base_url": None}

    async def fake_get_default(db, purpose="generation"):
        return cfg
    monkeypatch.setattr(ai_provider_service, "get_default_provider_config", fake_get_default)

    async def fake_acompletion(**kwargs):
        return _fake_stream(*[_Chunk(c) for c in _CHUNKS], _Chunk(None, usage=_Usage(10, 20)))
    monkeypatch.setattr(ai_service_mod, "acompletion", fake_acompletion)


def _collect(app_id, user_id, live_code):
    async def _run():
        events = []
        async with async_session() as db:
            async for ev in ai_service_mod.ai_service.chat(
                db, app_id, "show me the app", user_id=user_id, live_code=live_code
            ):
                events.append(ev)
        return events
    return asyncio.run(_run())


def test_live_code_streams_files_and_refs(client, admin_token, monkeypatch):
    app_id = str(uuid.uuid4())
    _insert_app(app_id, _admin_id())
    _wire(monkeypatch)

    events = _collect(app_id, _admin_id(), live_code=True)

    cs = [e["data"] for e in events if e["type"] == "code_stream"]
    assert any(e["event"] == "file_start" and e["path"] == "src/App.tsx" for e in cs)
    assert any(e["event"] == "file_end" and e["path"] == "src/App.tsx" for e in cs)
    body = "".join(e["text"] for e in cs if e["event"] == "delta")
    assert "export default function App()" in body

    done = [e["data"] for e in events if e["type"] == "done"][0]
    assert {"path": "src/App.tsx", "start": 1, "end": 3} in done["code_refs"]
    assert "[[jump" not in (done["description"] or "")     # directive stripped from prose


def test_no_code_stream_when_live_off_but_refs_still_present(client, admin_token, monkeypatch):
    app_id = str(uuid.uuid4())
    _insert_app(app_id, _admin_id())
    _wire(monkeypatch)

    events = _collect(app_id, _admin_id(), live_code=False)

    assert not any(e["type"] == "code_stream" for e in events)
    done = [e["data"] for e in events if e["type"] == "done"][0]
    # code_refs are independent of live_code — clickable chips work regardless.
    assert {"path": "src/App.tsx", "start": 1, "end": 3} in done["code_refs"]


def test_prose_identical_live_on_vs_off(client, admin_token, monkeypatch):
    app_id = str(uuid.uuid4())
    _insert_app(app_id, _admin_id())
    _wire(monkeypatch)

    def text(evs):
        return "".join(e["data"] for e in evs if e["type"] == "text")

    on = _collect(app_id, _admin_id(), live_code=True)
    off = _collect(app_id, _admin_id(), live_code=False)
    assert text(on) == text(off)
    assert "export default" not in text(on)               # code suppressed from the bubble


def test_ws_route_reads_live_code(client, admin_token, monkeypatch):
    """The only test that proves data.get('live_code') is read off the socket payload."""
    app_id = str(uuid.uuid4())
    _insert_app(app_id, _admin_id())
    _wire(monkeypatch)

    with client.websocket_connect("/api/ai/chat") as ws:
        ws.send_json({"token": admin_token})
        assert ws.receive_json()["type"] == "authenticated"
        ws.send_json({"app_id": app_id, "message": "show me the app", "live_code": True})
        seen = []
        for _ in range(200):
            msg = ws.receive_json()
            seen.append(msg["type"])
            if msg["type"] in ("done", "error"):
                break
    assert "code_stream" in seen
    assert "done" in seen
    assert "error" not in seen


def test_editor_context_reaches_the_llm(client, admin_token, monkeypatch):
    """The in-code overlay's editor_context must arrive as a focused system message in the
    actual LLM call — and be absent when not provided."""
    app_id = str(uuid.uuid4())
    _insert_app(app_id, _admin_id())

    captured = {}
    cfg = {"provider_type": "openai", "model": "gpt-4o-mini", "api_key": "x", "base_url": None}

    async def fake_get_default(db, purpose="generation"):
        return cfg
    monkeypatch.setattr(ai_provider_service, "get_default_provider_config", fake_get_default)

    async def fake_acompletion(**kwargs):
        captured["messages"] = kwargs.get("messages")
        return _fake_stream(_Chunk("Sure."), _Chunk(None, usage=_Usage(1, 1)))
    monkeypatch.setattr(ai_service_mod, "acompletion", fake_acompletion)

    def _system_text():
        return "\n".join(m["content"] for m in captured["messages"] if m["role"] == "system")

    def _run(editor_context):
        async def _go():
            async with async_session() as db:
                async for _ in ai_service_mod.ai_service.chat(
                    db, app_id, "what does this do?", user_id=_admin_id(), editor_context=editor_context
                ):
                    pass
        asyncio.run(_go())

    ctx = {
        "path": "src/App.tsx", "viewportStartLine": 5, "viewportEndLine": 25,
        "selectionText": "const total = revenue - cost", "selStartLine": 10, "selEndLine": 10,
    }
    _run(ctx)
    sys_text = _system_text()
    assert "What the user is looking at" in sys_text
    assert "src/App.tsx" in sys_text
    assert "const total = revenue - cost" in sys_text

    captured.clear()
    _run(None)
    assert "What the user is looking at" not in _system_text()


def test_conversation_history_restores_code_refs(client, admin_token, monkeypatch):
    """Reloading a conversation must show the SAME 'jump to code' chips it had live —
    derived from the stored response, with the raw [[jump:...]] directive stripped from the
    displayed text (so leaving + returning to the chat looks identical)."""
    app_id = str(uuid.uuid4())
    _insert_app(app_id, _admin_id())
    _wire(monkeypatch)

    # Persist a real assistant turn whose response contains a [[jump:...]] directive.
    _collect(app_id, _admin_id(), live_code=False)

    r = client.get(f"/api/ai/conversations/{app_id}", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200, r.text
    assistant = [m for m in r.json()["messages"] if m["role"] == "assistant"]
    assert assistant, "no assistant message persisted"
    m = assistant[-1]
    assert {"path": "src/App.tsx", "start": 1, "end": 3} in m["code_refs"]  # chips restored
    assert "[[jump" not in m["content"]                                      # directive stripped
