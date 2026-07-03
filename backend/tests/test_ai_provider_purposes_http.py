"""Phase-0 integration tests: per-purpose provider defaults + llm_usage gap closure.

Covers, via the REAL HTTP routes (TestClient, real login, no auth bypass):
  - GET/PUT /api/admin/ai-providers/purposes — pin/clear/model-override, the
    resolution chain (pinned > legacy boolean > inherited generation > first
    active), and that the api_key never leaks into the purposes payload.
  - The three call paths that used to be invisible in llm_usage now record:
    AI Toggle chat (purpose="ai_toggle", incl. an error row on LLM failure),
    the self-heal fix call (purpose="self_heal"), and the bug-report analyzer
    (purpose="bug_analysis", attributed to "(system)").

LLM calls are faked at the llm_compat seam — exactly the boundary the real
code goes through — so request routing, provider resolution, and usage
bookkeeping are all real.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_ai_provider_purposes.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_provider_purposes")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "provider-purposes-test")

from src.config import settings  # noqa: E402
from src.database import async_session, init_db  # noqa: E402
from src.main import app as fastapi_app  # noqa: E402
from src.ai_providers.purposes import PURPOSES  # noqa: E402

HAIKU = "claude-haiku-4-5-20251001"


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


@pytest.fixture(scope="module")
def providers(client, admin):
    """Two providers: p1 is the legacy generation default, p2 has no flags.
    Torn down (pins cleared, providers deleted) so a shared-process full-suite
    run doesn't leak default providers into other test modules."""
    p1 = client.post("/api/admin/ai-providers", json={
        "name": "Gen Provider", "provider_type": "openai", "api_key": "k1",
        "default_model": "gpt-5.4-mini", "is_default_generation": True,
    }, headers=admin).json()
    p2 = client.post("/api/admin/ai-providers", json={
        "name": "Cheap Provider", "provider_type": "anthropic", "api_key": "k2",
        "default_model": HAIKU,
    }, headers=admin).json()
    yield p1, p2
    for purpose in PURPOSES:
        client.put(f"/api/admin/ai-providers/purposes/{purpose}",
                   json={"provider_id": None}, headers=admin)
    for p in (p1, p2):
        client.delete(f"/api/admin/ai-providers/{p['id']}", headers=admin)


def _usage_rows(app_id: str) -> list[dict]:
    conn = sqlite3.connect(settings.database_url[len("sqlite+aiosqlite:///"):])
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT purpose, user_id, provider_type, model, input_tokens, output_tokens,"
            "       cost_usd, error FROM llm_usage WHERE app_id = ? ORDER BY created_at",
            (app_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _purposes_map(client, admin) -> dict[str, dict]:
    r = client.get("/api/admin/ai-providers/purposes", headers=admin)
    assert r.status_code == 200, r.text
    return {row["purpose"]: row for row in r.json()}


def _make_app(client, admin, name: str) -> str:
    r = client.post("/api/apps", json={"name": name}, headers=admin)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


# --------------------------------------------------------- purpose resolution

def test_purpose_catalog_and_inheritance(client, admin, providers):
    p1, _p2 = providers
    rows = _purposes_map(client, admin)
    assert set(rows) == set(PURPOSES)

    gen = rows["generation"]["effective"]
    assert gen["provider_id"] == p1["id"]
    assert gen["source"] == "legacy_default"

    for purpose in ("toggle", "bug_analysis", "marketplace_metadata"):
        eff = rows[purpose]["effective"]
        assert eff["provider_id"] == p1["id"], purpose
        assert eff["source"] == "inherited_generation", purpose

    # Decrypted keys must never reach this endpoint in any shape.
    assert "api_key" not in json.dumps(list(rows.values()))
    assert "k1" not in json.dumps(list(rows.values()))


def test_pin_model_override_and_clear(client, admin, providers):
    _p1, p2 = providers
    r = client.put(f"/api/admin/ai-providers/purposes/bug_analysis",
                   json={"provider_id": p2["id"], "model": HAIKU}, headers=admin)
    assert r.status_code == 200, r.text
    row = next(x for x in r.json() if x["purpose"] == "bug_analysis")
    assert row["provider_id"] == p2["id"]
    assert row["model"] == HAIKU
    assert row["effective"]["source"] == "pinned"
    assert row["effective"]["model"] == HAIKU

    # The pin drives real resolution (what analyzer calls), not just display.
    async def _resolve():
        from src.ai_providers.service import ai_provider_service
        async with async_session() as db:
            return await ai_provider_service.get_default_provider_config(db, "bug_analysis")
    cfg = asyncio.run(_resolve())
    assert cfg["provider_type"] == "anthropic"
    assert cfg["model"] == HAIKU
    assert cfg["api_key"] == "k2"  # decrypted for the caller, never for the API

    r = client.put("/api/admin/ai-providers/purposes/bug_analysis",
                   json={"provider_id": None}, headers=admin)
    row = next(x for x in r.json() if x["purpose"] == "bug_analysis")
    assert row["provider_id"] is None
    assert row["effective"]["source"] == "inherited_generation"


def test_pin_validation_and_auth(client, admin, providers):
    p1, _p2 = providers
    assert client.put("/api/admin/ai-providers/purposes/nonsense",
                      json={"provider_id": p1["id"]}, headers=admin).status_code == 404
    assert client.put("/api/admin/ai-providers/purposes/toggle",
                      json={"provider_id": "no-such-provider"}, headers=admin).status_code == 404
    assert client.put("/api/admin/ai-providers/purposes/toggle",
                      json={"model": "some-model"}, headers=admin).status_code == 422
    # Admin-only, like the rest of the provider API.
    assert client.get("/api/admin/ai-providers/purposes").status_code in (401, 403)


def test_toggle_legacy_boolean_beats_inheritance(client, admin, providers):
    p1, p2 = providers
    client.put(f"/api/admin/ai-providers/{p2['id']}",
               json={"is_default_toggle": True}, headers=admin)
    rows = _purposes_map(client, admin)
    assert rows["toggle"]["effective"]["provider_id"] == p2["id"]
    assert rows["toggle"]["effective"]["source"] == "legacy_default"
    # Other purposes keep inheriting generation, not the toggle default.
    assert rows["bug_analysis"]["effective"]["provider_id"] == p1["id"]
    client.put(f"/api/admin/ai-providers/{p2['id']}",
               json={"is_default_toggle": False}, headers=admin)


def test_first_active_fallback_when_no_defaults(client, admin, providers):
    p1, _p2 = providers
    client.put(f"/api/admin/ai-providers/{p1['id']}",
               json={"is_default_generation": False}, headers=admin)
    rows = _purposes_map(client, admin)
    assert rows["generation"]["effective"]["source"] == "first_active"
    assert rows["toggle"]["effective"]["source"] == "first_active"
    client.put(f"/api/admin/ai-providers/{p1['id']}",
               json={"is_default_generation": True}, headers=admin)


def test_purposes_endpoint_survives_undecryptable_keys(client, admin, providers, monkeypatch):
    """Key rotation must not 500 the purposes endpoints: display resolution
    never decrypts (decrypt=False), so a broken MASTER_ENCRYPTION_KEY leaves
    this page working — it's the page admins use to re-enter keys."""
    from src.secrets.encryption import encryption_service

    def broken_decrypt(_value):
        raise ValueError("Failed to decrypt secret. If the encryption key changed, re-enter the secret.")
    monkeypatch.setattr(encryption_service, "decrypt", broken_decrypt)

    rows = _purposes_map(client, admin)
    assert rows["generation"]["effective"] is not None


def test_delete_provider_clears_its_pins(client, admin, providers):
    p3 = client.post("/api/admin/ai-providers", json={
        "name": "Doomed Provider", "provider_type": "openai", "api_key": "k3",
        "default_model": "gpt-5.4-nano",
    }, headers=admin).json()
    client.put("/api/admin/ai-providers/purposes/toggle",
               json={"provider_id": p3["id"]}, headers=admin)
    assert _purposes_map(client, admin)["toggle"]["provider_id"] == p3["id"]

    assert client.delete(f"/api/admin/ai-providers/{p3['id']}", headers=admin).status_code == 204

    row = _purposes_map(client, admin)["toggle"]
    assert row["provider_id"] is None  # pin cleared, no dangling reference
    assert row["effective"]["source"] == "inherited_generation"


# --------------------------------------------------- fake LLM (llm_compat seam)

class _FakeUsage:
    def __init__(self, prompt_tokens=111, completion_tokens=22):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content, usage=True):
        self.choices = [_FakeChoice(content)]
        self.usage = _FakeUsage() if usage else None


# --------------------------------------------------------------- usage: toggle

def test_ai_toggle_chat_records_usage(client, admin, providers, monkeypatch):
    import src.llm_compat as llm_compat

    async def fake_acompletion(**kwargs):
        return _FakeResponse('{"response": "hi there", "actions": []}')
    monkeypatch.setattr(llm_compat, "acompletion", fake_acompletion)

    app_id = _make_app(client, admin, "Toggle Usage App")
    r = client.put(f"/api/apps/{app_id}", json={"ai_toggle_enabled": True}, headers=admin)
    assert r.status_code == 200, r.text

    r = client.post(f"/api/ai-toggle/{app_id}/chat", json={"message": "hello"}, headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["response"] == "hi there"

    rows = _usage_rows(app_id)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["purpose"] == "ai_toggle"
    assert row["input_tokens"] == 111 and row["output_tokens"] == 22
    assert row["error"] is None
    # Attributed to the authenticated caller, not a placeholder.
    assert row["user_id"] not in ("", "(unknown)", None)


def test_ai_toggle_garbage_actions_reply_records_one_row(client, admin, providers, monkeypatch):
    """A reply whose actions fail validation (params not a dict) degrades to a
    plain-text response and meters exactly ONE row — parsing used to raise out
    of the success path and add a second, spurious error row."""
    import src.llm_compat as llm_compat

    raw = json.dumps({"response": "done", "actions": [{"name": "filter", "params": "region=west"}]})

    async def fake_acompletion(**kwargs):
        return _FakeResponse(raw)
    monkeypatch.setattr(llm_compat, "acompletion", fake_acompletion)

    app_id = _make_app(client, admin, "Toggle Garbage Actions App")
    client.put(f"/api/apps/{app_id}", json={"ai_toggle_enabled": True}, headers=admin)

    r = client.post(f"/api/ai-toggle/{app_id}/chat", json={"message": "hello"}, headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["response"] == raw  # degraded to text, not an error
    assert r.json()["actions"] == []

    rows = _usage_rows(app_id)
    assert len(rows) == 1, rows
    assert rows[0]["error"] is None


def test_ai_toggle_error_records_error_row(client, admin, providers, monkeypatch):
    import src.llm_compat as llm_compat

    async def exploding_acompletion(**kwargs):
        raise RuntimeError("provider down")
    monkeypatch.setattr(llm_compat, "acompletion", exploding_acompletion)

    app_id = _make_app(client, admin, "Toggle Error App")
    client.put(f"/api/apps/{app_id}", json={"ai_toggle_enabled": True}, headers=admin)

    r = client.post(f"/api/ai-toggle/{app_id}/chat", json={"message": "hello"}, headers=admin)
    assert r.status_code == 200  # friendly error message, not a 500
    assert "error" in r.json()["response"].lower()

    rows = _usage_rows(app_id)
    assert len(rows) == 1, rows
    assert rows[0]["purpose"] == "ai_toggle"
    assert rows[0]["input_tokens"] == 0 and rows[0]["output_tokens"] == 0
    assert "provider down" in (rows[0]["error"] or "")


# ------------------------------------------------------------- usage: analyzer

def test_bug_analyzer_records_system_usage(client, admin, providers, monkeypatch):
    import src.llm_compat as llm_compat

    async def fake_acompletion(**kwargs):
        return _FakeResponse(json.dumps({
            "summary": "Null customer id", "root_cause": "form never sets it",
            "risk_level": "low", "fixes": [],
        }))
    monkeypatch.setattr(llm_compat, "acompletion", fake_acompletion)

    app_id = _make_app(client, admin, "Analyzer Usage App")
    src_dir = Path(settings.app_data_dir) / app_id / "draft" / "frontend" / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "App.tsx").write_text("export default function App() { return null }\n", encoding="utf-8")

    from src.bug_reports.analyzer import run_analysis

    async def _run():
        async with async_session() as db:
            return await run_analysis(
                db, app_id=app_id, version=None, bug_title="Save fails",
                bug_description="clicking save does nothing", captured_context={},
            )
    result = asyncio.run(_run())
    assert result.error is None, result.error

    rows = _usage_rows(app_id)
    assert len(rows) == 1, rows
    assert rows[0]["purpose"] == "bug_analysis"
    assert rows[0]["user_id"] == "(system)"
    assert rows[0]["input_tokens"] == 111 and rows[0]["output_tokens"] == 22


# ------------------------------------------------------------ usage: self-heal

def test_self_heal_fix_call_records_usage(client, admin, providers, monkeypatch):
    from src.ai import service as ai_mod
    from src.ai.verifier import VerifyError, VerifyResult
    from src.apps.models import Conversation

    failing = VerifyResult(
        stage_reached="tsc",
        errors=[VerifyError(stage="tsc", file="src/App.tsx", line=1, column=1,
                            code="TS2304", message="Cannot find name 'Foo'.")],
        summary="tsc failed",
    )

    async def fake_verify(app_id, level, runtime_enabled=True):
        return failing
    monkeypatch.setattr(ai_mod, "verify_app", fake_verify)

    async def fake_acompletion(**kwargs):
        # No FILE block on purpose: the loop records the fix call's usage, then
        # stops with "LLM produced no file changes".
        return _FakeResponse("I could not produce a fix.")
    monkeypatch.setattr(ai_mod, "acompletion", fake_acompletion)

    app_id = _make_app(client, admin, "Self Heal Usage App")

    async def _drive():
        async with async_session() as db:
            conv = Conversation(app_id=app_id)
            db.add(conv)
            await db.flush()  # default=lambda ids materialize at flush
            events = []
            async for ev in ai_mod.ai_service._self_heal_loop(
                db, conv, app_id, "original response", "tsc", 2,
                {"provider_type": "openai", "model": "gpt-5.4-mini",
                 "api_key": "k", "base_url": None},
                runtime_enabled=False, live_code=False, user_id="heal-tester",
            ):
                events.append(ev)
            # The usage row is recorded with commit=False and rides the turn's
            # transaction; in production the outer chat() commit lands it. This
            # commit plays that role.
            await db.commit()
            return events

    events = asyncio.run(_drive())
    assert any(ev["type"] == "_final_verify" for ev in events)

    rows = [r for r in _usage_rows(app_id) if r["purpose"] == "self_heal"]
    assert len(rows) == 1, rows
    assert rows[0]["user_id"] == "heal-tester"
    assert rows[0]["input_tokens"] == 111 and rows[0]["output_tokens"] == 22
    assert rows[0]["error"] is None
