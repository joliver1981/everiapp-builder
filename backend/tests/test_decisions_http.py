"""Phase-3 integration tests: aiDecide decisions end to end.

Real HTTP routes: manifest upsert (via the real _save_generated_files hook),
invoke with LLM/cache/fallback sources, user-scoped caching, prompt-as-data
edits, span parenting (ai.decision -> child ai.call), usage metering, auth.
LLM faked at llm_compat._acompletion_raw so the instrumented gateway is real.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_decisions_http.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_decisions")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "decisions-test")

from src.config import settings  # noqa: E402
from src.database import async_session, init_db  # noqa: E402
from src.main import app as fastapi_app  # noqa: E402

TRACE_ID = "decision-trace-01"

MANIFEST = [{
    "name": "classify_question",
    "description": "Is this a follow-up question?",
    "prompt": "Classify the user's question as follow_up or new_query.",
    "output_schema": {"enum": ["follow_up", "new_query"]},
    "fallback": "new_query",
    "cache_ttl_seconds": 0,
}]


@pytest.fixture(scope="module", autouse=True)
def _init():
    asyncio.run(init_db())
    yield


@pytest.fixture(scope="module")
def client():
    with TestClient(fastapi_app) as c:
        yield c


def _login(client, username):
    r = client.post("/api/auth/login", json={"username": username, "password": "password"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def admin(client):
    return _login(client, "admin")


@pytest.fixture(scope="module")
def developer(client):
    return _login(client, "developer")


@pytest.fixture(scope="module")
def provider(client, admin):
    p = client.post("/api/admin/ai-providers", json={
        "name": "Decision Provider", "provider_type": "openai", "api_key": "dk",
        "default_model": "gpt-5.4-mini", "is_default_generation": True,
    }, headers=admin).json()
    yield p
    client.delete(f"/api/admin/ai-providers/{p['id']}", headers=admin)


def _sqlite():
    conn = sqlite3.connect(settings.database_url[len("sqlite+aiosqlite:///"):])
    conn.row_factory = sqlite3.Row
    return conn


def _rows(sql, *args):
    conn = _sqlite()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _wait_spans(app_id, n, timeout=4.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = _rows("SELECT * FROM ai_spans WHERE app_id = ? ORDER BY created_at", app_id)
        if len(rows) >= n:
            return rows
        time.sleep(0.05)
    raise AssertionError(f"expected {n} spans, got {_rows('SELECT kind FROM ai_spans WHERE app_id = ?', app_id)}")


def _wait_span_kind(app_id, kind, timeout=4.0):
    """Wait for a span of a SPECIFIC kind, not merely for span count >= 1.

    `next(s for s in _wait_spans(app_id, 1) if s["kind"] == kind)` raced: a
    decision emits an ai.decision parent AND an ai.call child via async
    fire-and-forget flushes, and `_wait_spans(app_id, 1)` returns the moment
    EITHER lands. Under load the child could arrive first, so the generator
    found no ai.decision span yet and raised StopIteration. Poll for the kind
    we actually assert on instead.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = _rows("SELECT * FROM ai_spans WHERE app_id = ? AND kind = ? ORDER BY created_at", app_id, kind)
        if rows:
            return rows[0]
        time.sleep(0.05)
    raise AssertionError(
        f"no {kind} span for {app_id}; have "
        f"{_rows('SELECT kind FROM ai_spans WHERE app_id = ?', app_id)}"
    )


def _decision_app(client, admin, name: str, manifest=None) -> str:
    """App with decisions declared through the REAL generation-save hook."""
    app_id = client.post("/api/apps", json={"name": name}, headers=admin).json()["id"]

    from src.ai.code_parser import GeneratedFile
    from src.ai.service import ai_service

    async def _save():
        await ai_service._save_generated_files(app_id, [
            GeneratedFile(path="decisions.json", action="create",
                          content=json.dumps(manifest or MANIFEST)),
        ])
    asyncio.run(_save())
    return app_id


# ---------------------------------------------------------------- fake LLM

class _FakeUsage:
    prompt_tokens = 50
    completion_tokens = 5


class _FakeResponse:
    def __init__(self, content, finish_reason="stop"):
        self.choices = [type("C", (), {
            "message": type("M", (), {"content": content})(),
            "finish_reason": finish_reason,
        })()]
        self.usage = _FakeUsage()


@pytest.fixture()
def fake_llm(monkeypatch):
    """Returns the list of kwargs each LLM call received."""
    import src.llm_compat as llm_compat
    calls: list[dict] = []

    async def fake_raw(kwargs):
        calls.append(kwargs)
        return _FakeResponse('"follow_up"')
    monkeypatch.setattr(llm_compat, "_acompletion_raw", fake_raw)
    return calls


# ------------------------------------------------------------------- tests

def test_manifest_upsert_and_registry(client, admin, provider):
    app_id = _decision_app(client, admin, "Registry App")
    r = client.get(f"/api/decisions/{app_id}", headers=admin)
    assert r.status_code == 200, r.text
    [d] = r.json()
    assert d["name"] == "classify_question"
    assert d["fallback"] == "new_query"
    assert d["output_schema"] == {"enum": ["follow_up", "new_query"]}


def test_invalid_manifest_rejected_without_breaking_save(client, admin, provider):
    # Missing fallback -> manifest rejected, registry stays empty, no exception.
    app_id = _decision_app(client, admin, "Bad Manifest App",
                           manifest=[{"name": "x", "prompt": "p"}])
    assert client.get(f"/api/decisions/{app_id}", headers=admin).json() == []


def test_manifest_partial_validity_registers_good_entries(client, admin, provider):
    """One bad entry must not block the rest — all-or-nothing rejection used to
    cause registry drift (app calls a decision that was never registered)."""
    manifest = [MANIFEST[0], {"name": "broken_no_fallback", "prompt": "p"}]
    app_id = _decision_app(client, admin, "Partial Manifest App", manifest=manifest)
    names = [d["name"] for d in client.get(f"/api/decisions/{app_id}", headers=admin).json()]
    assert names == ["classify_question"]


def test_invoke_llm_path_with_spans_and_usage(client, admin, provider, fake_llm):
    app_id = _decision_app(client, admin, "Invoke App")
    r = client.post(f"/api/decisions/{app_id}/classify_question/invoke",
                    json={"input": {"question": "and per region?"}},
                    headers={**admin, "X-AIHub-Trace-Id": TRACE_ID})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["value"] == "follow_up" and body["source"] == "llm"

    # The model saw the registry prompt + schema note + the input JSON.
    messages = fake_llm[0]["messages"]
    assert "Classify the user's question" in messages[0]["content"]
    assert "JSON Schema" in messages[0]["content"]
    assert "and per region?" in messages[1]["content"]

    spans = {s["kind"]: s for s in _wait_spans(app_id, 2)}
    decision, call = spans["ai.decision"], spans["ai.call"]
    assert decision["name"] == "classify_question"
    assert decision["trace_id"] == TRACE_ID and call["trace_id"] == TRACE_ID
    assert call["parent_span_id"] == decision["id"]  # nesting for the story view

    [usage] = _rows("SELECT purpose, span_id, trace_id FROM llm_usage WHERE app_id = ?", app_id)
    assert usage["purpose"] == "decision"
    assert usage["span_id"] == call["id"] and usage["trace_id"] == TRACE_ID


def test_fallback_on_llm_error(client, admin, provider, monkeypatch):
    import src.llm_compat as llm_compat

    async def exploding(kwargs):
        raise RuntimeError("model down")
    monkeypatch.setattr(llm_compat, "_acompletion_raw", exploding)

    app_id = _decision_app(client, admin, "Fallback App")
    r = client.post(f"/api/decisions/{app_id}/classify_question/invoke",
                    json={"input": {"q": 1}}, headers=admin)
    assert r.status_code == 200  # never a 5xx for LLM trouble
    assert r.json() == {"value": "new_query", "source": "fallback",
                        "latency_ms": r.json()["latency_ms"]}
    span = _wait_span_kind(app_id, "ai.decision")
    assert span["status"] == "error" and "model down" in span["error"]


def test_timeout_error_is_self_documenting(client, admin, provider, monkeypatch):
    """A timed-out decision must name the knob (timeout_seconds) and how to
    raise it — 'TimeoutError:' alone sent a real debugging session guessing
    about a nonexistent 'platform execution budget'."""
    import src.llm_compat as llm_compat

    async def slow(kwargs):
        await asyncio.sleep(3)
        return _FakeResponse('"follow_up"')
    monkeypatch.setattr(llm_compat, "_acompletion_raw", slow)

    manifest = [dict(MANIFEST[0], timeout_seconds=1)]
    app_id = _decision_app(client, admin, "Timeout Doc App", manifest=manifest)
    r = client.post(f"/api/decisions/{app_id}/classify_question/invoke",
                    json={"input": {}}, headers=admin)
    assert r.json()["source"] == "fallback"
    span = _wait_span_kind(app_id, "ai.decision")
    assert "timed out after 1s" in span["error"]
    assert "timeout_seconds" in span["error"]  # the knob, by name


def test_fallback_on_schema_violation(client, admin, provider, monkeypatch):
    import src.llm_compat as llm_compat

    async def off_menu(kwargs):
        return _FakeResponse('"maybe_followup"')
    monkeypatch.setattr(llm_compat, "_acompletion_raw", off_menu)

    app_id = _decision_app(client, admin, "Schema App")
    r = client.post(f"/api/decisions/{app_id}/classify_question/invoke",
                    json={"input": {}}, headers=admin)
    assert r.json()["source"] == "fallback"
    assert r.json()["value"] == "new_query"
    span = _wait_span_kind(app_id, "ai.decision")
    assert "schema" in (span["error"] or "")


def test_cache_is_user_scoped(client, admin, developer, provider, fake_llm):
    manifest = [dict(MANIFEST[0], cache_ttl_seconds=300)]
    app_id = _decision_app(client, admin, "Cache App", manifest=manifest)
    url = f"/api/decisions/{app_id}/classify_question/invoke"
    body = {"input": {"question": "same input"}}

    assert client.post(url, json=body, headers=admin).json()["source"] == "llm"
    assert client.post(url, json=body, headers=admin).json()["source"] == "cache"
    assert len(fake_llm) == 1  # second call never reached the model
    # A DIFFERENT user must not get the cached answer (results could embed
    # user-specific data) — the cache key is user-scoped.
    assert client.post(url, json=body, headers=developer).json()["source"] == "llm"
    assert len(fake_llm) == 2


def test_prompt_edit_applies_immediately_and_busts_cache(client, admin, provider, fake_llm):
    manifest = [dict(MANIFEST[0], cache_ttl_seconds=300)]
    app_id = _decision_app(client, admin, "Edit App", manifest=manifest)
    url = f"/api/decisions/{app_id}/classify_question/invoke"
    body = {"input": {"q": "x"}}
    client.post(url, json=body, headers=admin)

    r = client.put(f"/api/decisions/{app_id}/classify_question",
                   json={"prompt": "NEW TUNED PROMPT v2"}, headers=admin)
    assert r.status_code == 200 and r.json()["prompt_template"] == "NEW TUNED PROMPT v2"

    # Same input: cache invalidated (prompt hash in key) + new prompt used.
    assert client.post(url, json=body, headers=admin).json()["source"] == "llm"
    assert "NEW TUNED PROMPT v2" in fake_llm[-1]["messages"][0]["content"]

    # Re-running the generator manifest must NOT clobber the tuned prompt.
    from src.ai.code_parser import GeneratedFile
    from src.ai.service import ai_service

    async def _resave():
        await ai_service._save_generated_files(app_id, [
            GeneratedFile(path="decisions.json", action="create",
                          content=json.dumps(manifest)),
        ])
    asyncio.run(_resave())
    d = client.get(f"/api/decisions/{app_id}", headers=admin).json()[0]
    assert d["prompt_template"] == "NEW TUNED PROMPT v2"


def test_fenced_json_answer_accepted(client, admin, provider, monkeypatch):
    """Models love ```json fences despite instructions — strip and parse."""
    import src.llm_compat as llm_compat

    async def fenced(kwargs):
        return _FakeResponse('```json\n"follow_up"\n```')
    monkeypatch.setattr(llm_compat, "_acompletion_raw", fenced)

    app_id = _decision_app(client, admin, "Fenced App")
    r = client.post(f"/api/decisions/{app_id}/classify_question/invoke",
                    json={"input": {}}, headers=admin)
    assert r.json()["value"] == "follow_up" and r.json()["source"] == "llm"


def test_truncated_output_error_is_self_documenting(client, admin, provider, monkeypatch):
    """The real-world failure: output cut off at max_tokens mid-JSON. The
    span must say 'truncated', name the knob (max_output_tokens) AND how to
    raise it — not just 'unparseable' or 'reduce your prompt'."""
    import src.llm_compat as llm_compat

    async def truncated(kwargs):
        return _FakeResponse('```json\n{"system_prompts": ["You are', finish_reason="length")
    monkeypatch.setattr(llm_compat, "_acompletion_raw", truncated)

    app_id = _decision_app(client, admin, "Truncated App")
    r = client.post(f"/api/decisions/{app_id}/classify_question/invoke",
                    json={"input": {}}, headers=admin)
    assert r.json()["source"] == "fallback"
    span = _wait_span_kind(app_id, "ai.decision")
    assert "truncated" in span["error"]
    assert "max_output_tokens" in span["error"]          # the knob, by name
    assert "reduce the requested volume" in span["error"]


def test_default_output_ceiling_inherits_platform_default(client, admin, provider, fake_llm):
    """A decision declared without max_output_tokens stores NULL (inherit) and
    uses the generous platform default (16384) at call time — not the old
    brittle 4096."""
    app_id = _decision_app(client, admin, "Default Ceiling App")
    d = client.get(f"/api/decisions/{app_id}", headers=admin).json()[0]
    assert d["max_output_tokens"] is None            # inherits, not pinned

    client.post(f"/api/decisions/{app_id}/classify_question/invoke",
                json={"input": {}}, headers=admin)
    assert fake_llm[-1]["max_tokens"] == 16384       # the platform default


def test_admin_platform_default_is_editable_and_applies(client, admin, provider, fake_llm):
    """The knob the AI tells users to ask their admin about: raising the
    platform-wide decision ceiling in Settings immediately raises the effective
    cap for every decision that inherits (max_output_tokens NULL)."""
    app_id = _decision_app(client, admin, "Inherit Platform App")
    try:
        r = client.put("/api/admin/settings", json={"decision_max_output_tokens": 40000}, headers=admin)
        assert r.status_code == 200, r.text
        assert r.json()["decision_max_output_tokens"] == 40000

        client.post(f"/api/decisions/{app_id}/classify_question/invoke",
                    json={"input": {}}, headers=admin)
        assert fake_llm[-1]["max_tokens"] == 40000   # inherited the raised default
    finally:
        client.put("/api/admin/settings", json={"decision_max_output_tokens": 16384}, headers=admin)


def test_per_decision_override_beats_platform_default(client, admin, provider, fake_llm):
    """An explicit per-decision max_output_tokens wins over the platform default."""
    manifest = [dict(MANIFEST[0], max_output_tokens=8000)]
    app_id = _decision_app(client, admin, "Override Wins App", manifest=manifest)
    try:
        client.put("/api/admin/settings", json={"decision_max_output_tokens": 40000}, headers=admin)
        client.post(f"/api/decisions/{app_id}/classify_question/invoke",
                    json={"input": {}}, headers=admin)
        assert fake_llm[-1]["max_tokens"] == 8000    # the decision's own value, not 40000
    finally:
        client.put("/api/admin/settings", json={"decision_max_output_tokens": 16384}, headers=admin)


def test_manifest_can_raise_output_ceiling(client, admin, provider, fake_llm):
    """A generative decision (side-by-side model comparison) declares a big
    ceiling in decisions.json; it reaches the LLM call so output isn't truncated."""
    manifest = [dict(MANIFEST[0], max_output_tokens=48000)]
    app_id = _decision_app(client, admin, "Big Ceiling App", manifest=manifest)
    assert client.get(f"/api/decisions/{app_id}", headers=admin).json()[0]["max_output_tokens"] == 48000

    client.post(f"/api/decisions/{app_id}/classify_question/invoke",
                json={"input": {}}, headers=admin)
    assert fake_llm[-1]["max_tokens"] == 48000


def test_output_ceiling_editable_via_put_within_bounds(client, admin, provider):
    """PUT sets max_output_tokens (takes effect next invocation). The API bound
    is a sane ceiling above any current model's output — over it is a clean 422,
    not a silent truncation-to-fallback later."""
    app_id = _decision_app(client, admin, "Editable Ceiling App")
    # A modest bound set by an operator who WANTS to cap cost is honored.
    r = client.put(f"/api/decisions/{app_id}/classify_question",
                   json={"max_output_tokens": 512}, headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["max_output_tokens"] == 512

    # Above the platform ceiling: rejected up front (422), not accepted then
    # truncated at model time.
    r = client.put(f"/api/decisions/{app_id}/classify_question",
                   json={"max_output_tokens": 999999}, headers=admin)
    assert r.status_code == 422


def test_manifest_output_ceiling_clamps_silently(client, admin, provider):
    """Generated decisions.json is code — an out-of-range value must clamp to the
    platform max, never fail the manifest (which would drift the registry)."""
    manifest = [dict(MANIFEST[0], max_output_tokens=999999)]
    app_id = _decision_app(client, admin, "Clamp Manifest App", manifest=manifest)
    d = client.get(f"/api/decisions/{app_id}", headers=admin).json()[0]
    assert d["name"] == "classify_question"        # manifest still registered
    assert d["max_output_tokens"] == 64000         # clamped to the ceiling


def test_bare_enum_answer_accepted(client, admin, provider, monkeypatch):
    """Cheap models often answer `follow_up` without JSON quoting — that must
    count as a valid enum answer, not burn the fallback."""
    import src.llm_compat as llm_compat

    async def bare(kwargs):
        return _FakeResponse("follow_up")
    monkeypatch.setattr(llm_compat, "_acompletion_raw", bare)

    app_id = _decision_app(client, admin, "Bare Enum App")
    r = client.post(f"/api/decisions/{app_id}/classify_question/invoke",
                    json={"input": {}}, headers=admin)
    assert r.json() == {"value": "follow_up", "source": "llm",
                        "latency_ms": r.json()["latency_ms"]}


def test_manifest_resave_preserves_admin_knobs(client, admin, provider):
    """Re-saving the manifest (regeneration, self-heal) must not clobber the
    admin-tuned model/temperature/cache TTL — only seed them on create."""
    manifest = [dict(MANIFEST[0], cache_ttl_seconds=60, temperature=0.5)]
    app_id = _decision_app(client, admin, "Knobs App", manifest=manifest)
    client.put(f"/api/decisions/{app_id}/classify_question",
               json={"model": "claude-haiku-4-5-20251001", "temperature": 0.1,
                     "cache_ttl_seconds": 900, "timeout_seconds": 45}, headers=admin)

    from src.ai.code_parser import GeneratedFile
    from src.ai.service import ai_service

    async def _resave():
        await ai_service._save_generated_files(app_id, [
            GeneratedFile(path="decisions.json", action="create",
                          content=json.dumps(manifest)),
        ])
    asyncio.run(_resave())

    d = client.get(f"/api/decisions/{app_id}", headers=admin).json()[0]
    assert d["model"] == "claude-haiku-4-5-20251001"
    assert d["temperature"] == 0.1
    assert d["cache_ttl_seconds"] == 900
    assert d["timeout_seconds"] == 45


def test_invoke_rate_limited(client, admin, provider, fake_llm, monkeypatch):
    from src import rate_limit
    monkeypatch.setattr(rate_limit.decision_limiter, "capacity", 2)
    rate_limit.decision_limiter._buckets.clear()

    app_id = _decision_app(client, admin, "Limited App")
    url = f"/api/decisions/{app_id}/classify_question/invoke"
    statuses = [client.post(url, json={"input": {"i": i}}, headers=admin).status_code
                for i in range(4)]
    assert 429 in statuses  # the bucket runs dry
    rate_limit.decision_limiter._buckets.clear()


def test_sync_endpoint_heals_registry_drift(client, admin, provider):
    """The drift found in real testing: manifest on disk, registry empty
    (written under an older backend). POST /sync re-registers from disk."""
    app_id = client.post("/api/apps", json={"name": "Drift App"}, headers=admin).json()["id"]
    draft = Path(settings.app_data_dir) / app_id / "draft" / "frontend"
    draft.mkdir(parents=True, exist_ok=True)
    (draft / "decisions.json").write_text(json.dumps(MANIFEST), encoding="utf-8")

    assert client.get(f"/api/decisions/{app_id}", headers=admin).json() == []
    r = client.post(f"/api/decisions/{app_id}/sync", headers=admin)
    assert r.status_code == 200, r.text
    assert r.json() == {"registered": ["classify_question"], "errors": []}
    assert [d["name"] for d in client.get(f"/api/decisions/{app_id}", headers=admin).json()] \
        == ["classify_question"]

    # Preview start runs the same sync automatically (fresh app, src/ variant).
    app2 = client.post("/api/apps", json={"name": "Drift App 2"}, headers=admin).json()["id"]
    draft2 = Path(settings.app_data_dir) / app2 / "draft" / "frontend" / "src"
    draft2.mkdir(parents=True, exist_ok=True)
    (draft2 / "decisions.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
    import src.runtime.manager as rt

    async def fake_start(app_id, source="draft"):
        return None
    orig = rt.runtime_manager.start_app
    rt.runtime_manager.start_app = fake_start
    try:
        assert client.post(f"/api/apps/{app2}/runtime/start", headers=admin).status_code == 200
    finally:
        rt.runtime_manager.start_app = orig
    assert [d["name"] for d in client.get(f"/api/decisions/{app2}", headers=admin).json()] \
        == ["classify_question"]


def test_decision_input_is_unlimited_by_default(client, admin, provider, fake_llm):
    """No decision INPUT cap out of the box — a large input reaches the model
    instead of bouncing off the old hardcoded 20,000-char ceiling."""
    app_id = _decision_app(client, admin, "Big Input App")
    big = "x" * 50_000  # far above the removed 20k hardcoded cap
    r = client.post(f"/api/decisions/{app_id}/classify_question/invoke",
                    json={"input": {"doc": big}}, headers=admin)
    assert r.json()["source"] == "llm"          # accepted, not "input too large"
    assert fake_llm, "the large input should have reached the model"


def test_admin_can_opt_into_a_decision_input_cap(client, admin, provider, fake_llm):
    """An operator who wants cost control can set an input cap; over it, the
    decision returns its fallback with a self-documenting error naming the knob."""
    app_id = _decision_app(client, admin, "Capped Input App")
    try:
        client.put("/api/admin/settings", json={"decision_max_input_chars": 100}, headers=admin)
        r = client.post(f"/api/decisions/{app_id}/classify_question/invoke",
                        json={"input": {"doc": "y" * 500}}, headers=admin)
        assert r.json()["source"] == "fallback"
        span = _wait_span_kind(app_id, "ai.decision")
        assert "input too large" in span["error"]
        assert "decision_max_input_chars" in span["error"]   # the knob, by name
    finally:
        client.put("/api/admin/settings", json={"decision_max_input_chars": 0}, headers=admin)


def test_delete_app_with_decisions(client, admin, provider, fake_llm):
    """Deleting an app that registered a decision must succeed. The
    app_decisions FK has no ondelete=CASCADE, so DELETE FROM apps used to hit a
    FOREIGN KEY constraint failure (500) for every aiDecide app — delete_app now
    clears decision rows first."""
    manifest = [dict(MANIFEST[0], cache_ttl_seconds=300)]
    app_id = _decision_app(client, admin, "Deletable Decision App", manifest=manifest)
    # Invoke once (fake LLM) so a decision_cache row exists too — the deeper child.
    r = client.post(f"/api/decisions/{app_id}/classify_question/invoke",
                    json={"input": {"q": "cache me"}}, headers=admin)
    assert r.json()["source"] == "llm"
    decision_ids = [row["id"] for row in _rows("SELECT id FROM app_decisions WHERE app_id = ?", app_id)]
    assert decision_ids and _rows(
        "SELECT id FROM decision_cache WHERE decision_id = ?", decision_ids[0])

    assert client.delete(f"/api/apps/{app_id}", headers=admin).status_code == 204
    # App and its decision + cache rows are gone (no orphans, no 500).
    assert client.get(f"/api/apps/{app_id}", headers=admin).status_code == 404
    assert _rows("SELECT id FROM app_decisions WHERE app_id = ?", app_id) == []
    assert _rows("SELECT id FROM decision_cache WHERE decision_id = ?", decision_ids[0]) == []


def test_auth_and_unknowns(client, admin, provider):
    app_id = _decision_app(client, admin, "Auth App")
    url = f"/api/decisions/{app_id}/classify_question/invoke"
    assert client.post(url, json={"input": {}}).status_code == 401  # costs tokens — attributable only
    assert client.post(f"/api/decisions/{app_id}/nope/invoke",
                       json={"input": {}}, headers=admin).status_code == 404
    assert client.post("/api/decisions/no-app/classify_question/invoke",
                       json={"input": {}}, headers=admin).status_code == 404
