"""Phase-1 trace-spine integration tests.

Real HTTP routes end to end: the X-AIHub-Trace-Id header flows through the
middleware contextvar into ai_spans rows emitted by the instrumented
llm_compat gateway, llm_usage rows join via trace_id/span_id, capture levels
apply at write time, payloads are encrypted at rest, retention sweeps, and
the spans read endpoint exposes metadata only.

The LLM is faked at llm_compat._acompletion_raw — one level BELOW the
instrumented acompletion(), so the span-emission path under test is real.
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
_DB = _TMP / "test_tracing_http.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_tracing")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "tracing-test")

from src.config import settings  # noqa: E402
from src.database import async_session, init_db  # noqa: E402
from src.main import app as fastapi_app  # noqa: E402

TRACE_ID = "trace-e2e-0001"


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
def plain_user(client):
    r = client.post("/api/auth/login", json={"username": "user", "password": "password"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def provider(client, admin):
    p = client.post("/api/admin/ai-providers", json={
        "name": "Trace Provider", "provider_type": "openai", "api_key": "trace-k",
        "default_model": "gpt-5.4-mini", "is_default_generation": True,
    }, headers=admin).json()
    yield p
    client.delete(f"/api/admin/ai-providers/{p['id']}", headers=admin)


def _sqlite():
    conn = sqlite3.connect(settings.database_url[len("sqlite+aiosqlite:///"):])
    conn.row_factory = sqlite3.Row
    return conn


def _spans(app_id: str) -> list[dict]:
    conn = _sqlite()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM ai_spans WHERE app_id = ? ORDER BY created_at", (app_id,)
        ).fetchall()]
    finally:
        conn.close()


def _wait_for_spans(app_id: str, n: int, timeout: float = 4.0) -> list[dict]:
    """The span writer is async — poll until n rows landed (or fail loudly)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = _spans(app_id)
        if len(rows) >= n:
            return rows
        time.sleep(0.05)
    raise AssertionError(f"expected {n} spans for {app_id}, got {_spans(app_id)}")


def _usage(app_id: str) -> list[dict]:
    conn = _sqlite()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT purpose, trace_id, span_id, input_tokens, output_tokens, error "
            "FROM llm_usage WHERE app_id = ? ORDER BY created_at", (app_id,)
        ).fetchall()]
    finally:
        conn.close()


def _toggle_app(client, admin, name: str) -> str:
    r = client.post("/api/apps", json={"name": name}, headers=admin)
    app_id = r.json()["id"]
    assert client.put(f"/api/apps/{app_id}", json={"ai_toggle_enabled": True},
                      headers=admin).status_code == 200
    return app_id


def _set_capture_level(level: str) -> None:
    async def _run():
        from src.platform_settings.service import set_setting
        async with async_session() as db:
            await set_setting(db, "trace_capture_level", level)
    asyncio.run(_run())


# ------------------------------------------------------------------ fake LLM

class _FakeUsage:
    prompt_tokens = 111
    completion_tokens = 22


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]
        self.usage = _FakeUsage()


@pytest.fixture()
def fake_llm(monkeypatch):
    import src.llm_compat as llm_compat

    async def fake_raw(kwargs):
        return _FakeResponse('{"response": "traced reply", "actions": []}')
    monkeypatch.setattr(llm_compat, "_acompletion_raw", fake_raw)


# ------------------------------------------------------------------ the spine

def test_trace_header_flows_to_span_and_usage(client, admin, provider, fake_llm):
    _set_capture_level("full")
    app_id = _toggle_app(client, admin, "Trace Spine App")

    r = client.post(f"/api/ai-toggle/{app_id}/chat", json={"message": "hello trace"},
                    headers={**admin, "X-AIHub-Trace-Id": TRACE_ID})
    assert r.status_code == 200, r.text
    assert r.headers.get("x-aihub-trace-id") == TRACE_ID  # echoed for correlation

    spans = _wait_for_spans(app_id, 1)
    span = spans[0]
    assert span["trace_id"] == TRACE_ID
    assert span["kind"] == "ai.call"
    assert span["purpose"] == "ai_toggle"
    assert span["status"] == "ok"
    assert span["provider_type"] == "openai"
    assert span["model"] == "gpt-5.4-mini"
    assert span["input_tokens"] == 111 and span["output_tokens"] == 22
    assert span["capture_level"] == "full"
    assert span["prompt_ct"] and span["response_ct"]

    usage = _usage(app_id)
    assert len(usage) == 1
    assert usage[0]["trace_id"] == TRACE_ID
    assert usage[0]["span_id"] == span["id"]  # cost row joins to exactly this span


def test_payloads_encrypted_at_rest(client, admin, provider, fake_llm):
    from src.secrets.encryption import encryption_service

    _set_capture_level("full")
    app_id = _toggle_app(client, admin, "Trace Encrypted App")
    client.post(f"/api/ai-toggle/{app_id}/chat", json={"message": "SECRET-NEEDLE-42"},
                headers={**admin, "X-AIHub-Trace-Id": TRACE_ID})
    span = _wait_for_spans(app_id, 1)[0]

    # Ciphertext at rest: plaintext must not appear anywhere in the row.
    assert "SECRET-NEEDLE-42" not in json.dumps(span, default=str)
    # ...but the platform can decrypt it (Phase 2 viewer path).
    assert "SECRET-NEEDLE-42" in encryption_service.decrypt(span["prompt_ct"])
    assert "traced reply" in encryption_service.decrypt(span["response_ct"])


def test_malformed_trace_header_is_dropped_not_fatal(client, admin, provider, fake_llm):
    _set_capture_level("full")
    app_id = _toggle_app(client, admin, "Trace Malformed App")
    r = client.post(f"/api/ai-toggle/{app_id}/chat", json={"message": "hi"},
                    headers={**admin, "X-AIHub-Trace-Id": "<script>alert(1)</script>"})
    assert r.status_code == 200
    assert "x-aihub-trace-id" not in {k.lower() for k in r.headers}
    span = _wait_for_spans(app_id, 1)[0]
    assert span["trace_id"] is None  # traced anonymously, not rejected


def test_capture_level_metadata_only_strips_payloads(client, admin, provider, fake_llm):
    _set_capture_level("metadata_only")
    try:
        app_id = _toggle_app(client, admin, "Trace Metadata App")
        client.post(f"/api/ai-toggle/{app_id}/chat", json={"message": "hi"},
                    headers={**admin, "X-AIHub-Trace-Id": TRACE_ID})
        span = _wait_for_spans(app_id, 1)[0]
        assert span["capture_level"] == "metadata_only"
        assert span["prompt_ct"] is None and span["response_ct"] is None
        assert span["input_tokens"] == 111  # metering still intact
    finally:
        _set_capture_level("full")


def test_capture_level_off_writes_no_spans(client, admin, provider, fake_llm):
    _set_capture_level("off")
    try:
        app_id = _toggle_app(client, admin, "Trace Off App")
        r = client.post(f"/api/ai-toggle/{app_id}/chat", json={"message": "hi"},
                        headers={**admin, "X-AIHub-Trace-Id": TRACE_ID})
        assert r.status_code == 200
        time.sleep(0.6)  # give the writer a chance to (wrongly) write
        assert _spans(app_id) == []
        assert len(_usage(app_id)) == 1  # cost metering is independent of spans
    finally:
        _set_capture_level("full")


def test_llm_error_produces_error_span(client, admin, provider, monkeypatch):
    import src.llm_compat as llm_compat

    async def exploding_raw(kwargs):
        raise RuntimeError("provider melted")
    monkeypatch.setattr(llm_compat, "_acompletion_raw", exploding_raw)

    _set_capture_level("full")
    app_id = _toggle_app(client, admin, "Trace Error App")
    r = client.post(f"/api/ai-toggle/{app_id}/chat", json={"message": "hi"},
                    headers={**admin, "X-AIHub-Trace-Id": TRACE_ID})
    assert r.status_code == 200  # toggle degrades to a friendly message

    span = _wait_for_spans(app_id, 1)[0]
    assert span["status"] == "error"
    assert "provider melted" in (span["error"] or "")
    assert span["response_ct"] is None  # there was no response
    assert span["prompt_ct"]  # the prompt is still captured for debugging


def test_retention_sweep_deletes_old_spans(client, admin, provider, fake_llm):
    _set_capture_level("full")
    app_id = _toggle_app(client, admin, "Trace Retention App")
    client.post(f"/api/ai-toggle/{app_id}/chat", json={"message": "hi"}, headers=admin)
    _wait_for_spans(app_id, 1)

    conn = _sqlite()
    try:
        conn.execute("UPDATE ai_spans SET created_at = datetime('now', '-30 days') WHERE app_id = ?",
                     (app_id,))
        conn.commit()
    finally:
        conn.close()

    async def _sweep():
        from src.tracing.service import retention_sweep
        async with async_session() as db:
            return await retention_sweep(db)
    deleted = asyncio.run(_sweep())
    assert deleted >= 1
    assert _spans(app_id) == []


def _widget_app(client, admin, name: str) -> str:
    """An app with the bug/telemetry widget enabled — the opt-in that anonymous
    span ingestion shares with anonymous bug reports."""
    r = client.post("/api/apps", json={"name": name}, headers=admin)
    app_id = r.json()["id"]
    assert client.put(f"/api/apps/{app_id}", json={"bug_widget_enabled": True},
                      headers=admin).status_code == 200
    return app_id


def test_ingest_client_spans_anonymous(client, admin):
    _set_capture_level("full")
    app_id = _widget_app(client, admin, "Ingest App")

    r = client.post(f"/api/apps/{app_id}/spans", json={"spans": [
        {"kind": "dataset.query", "name": "recent_orders", "trace_id": TRACE_ID,
         "status": "ok", "latency_ms": 42, "detail": '{"params": {"region": "west"}}'},
        {"kind": "ui.error", "name": "OrderForm", "trace_id": TRACE_ID,
         "status": "error", "error": "TypeError: x is undefined", "latency_ms": 0},
        {"kind": "not.a.kind", "name": "ignored"},
    ]})  # no auth header on purpose — deployed apps can be tokenless
    assert r.status_code == 202, r.text
    assert r.json()["accepted"] == 2

    rows = _wait_for_spans(app_id, 2)
    by_kind = {s["kind"]: s for s in rows}
    ds = by_kind["dataset.query"]
    assert ds["purpose"] == "app_runtime" and ds["trace_id"] == TRACE_ID
    assert ds["user_id"] is None and ds["latency_ms"] == 42
    assert ds["prompt_ct"]  # detail captured (encrypted) under full
    assert "region" not in json.dumps(rows, default=str)  # ...but not in plaintext
    err = by_kind["ui.error"]
    assert err["status"] == "error" and "TypeError" in err["error"]


def test_ingest_validation(client, admin):
    app_id = _widget_app(client, admin, "Ingest Validation App")
    too_many = {"spans": [{"kind": "ui.error"}] * 101}
    assert client.post(f"/api/apps/{app_id}/spans", json=too_many).status_code == 422
    assert client.post(f"/api/apps/{app_id}/spans", json={"spans": []}).status_code == 422
    assert client.post("/api/apps/no-such-app/spans",
                       json={"spans": [{"kind": "ui.error"}]}).status_code == 404
    # Malformed trace ids are dropped to NULL, not rejected.
    r = client.post(f"/api/apps/{app_id}/spans", json={"spans": [
        {"kind": "ui.interaction", "name": "Save", "trace_id": "<bad id>"}]})
    assert r.status_code == 202
    span = _wait_for_spans(app_id, 1)[0]
    assert span["trace_id"] is None


def test_ingest_anonymous_requires_widget_optin(client, admin):
    """Anonymous span writes share the bug-widget opt-in; a bearer bypasses it."""
    r = client.post("/api/apps", json={"name": "No Optin App"}, headers=admin)
    app_id = r.json()["id"]  # widget NOT enabled
    body = {"spans": [{"kind": "ui.error", "name": "boom", "status": "error"}]}
    assert client.post(f"/api/apps/{app_id}/spans", json=body).status_code == 403
    # Authenticated callers (builder preview) are always allowed.
    assert client.post(f"/api/apps/{app_id}/spans", json=body, headers=admin).status_code == 202


def test_ingest_anonymous_throttle(client, admin, monkeypatch):
    from src.tracing import router as tracing_router
    monkeypatch.setattr(tracing_router, "_ANON_SPANS_PER_MINUTE", 5)
    tracing_router._anon_windows.clear()

    app_id = _widget_app(client, admin, "Throttle App")
    body = {"spans": [{"kind": "ui.interaction", "name": "x"}] * 6}
    assert client.post(f"/api/apps/{app_id}/spans", json=body).status_code == 429
    # The budget applies only to the anonymous path.
    assert client.post(f"/api/apps/{app_id}/spans", json=body, headers=admin).status_code == 202


def test_metadata_only_redacts_client_free_text(client, admin):
    """Under metadata_only, client error text is bounded and click labels —
    which mirror on-screen user data — are redacted."""
    _set_capture_level("metadata_only")
    try:
        app_id = _widget_app(client, admin, "Redact App")
        r = client.post(f"/api/apps/{app_id}/spans", json={"spans": [
            {"kind": "ui.interaction", "name": "John Smith — john@acme.com"},
            {"kind": "dataset.query", "name": "orders", "status": "error",
             "error": "E" * 500},
        ]})
        assert r.status_code == 202
        rows = {s["kind"]: s for s in _wait_for_spans(app_id, 2)}
        assert rows["ui.interaction"]["name"] == "(interaction)"
        assert len(rows["dataset.query"]["error"]) == 200
    finally:
        _set_capture_level("full")


def test_bug_report_intake_keeps_trace_context(client, admin):
    """The intake schema must NOT strip trace_id/recent_spans (pydantic default
    extra='ignore' made the analyzer's traced-events section dead code)."""
    app_id = _widget_app(client, admin, "Bug Trace App")
    r = client.post(f"/api/bug-reports/{app_id}", json={
        "title": "Save does nothing",
        "captured_context": {
            "page_url": "http://x/",
            "trace_id": TRACE_ID,
            "recent_spans": [
                {"kind": "ui.interaction", "name": "Save order", "status": "ok",
                 "latency_ms": 0, "ts": 1},
                {"kind": "dataset.query", "name": "insert_order", "status": "error",
                 "error": "NOT NULL constraint failed", "latency_ms": 240, "ts": 2},
            ],
        },
    })
    assert r.status_code in (200, 201, 202), r.text

    conn = _sqlite()
    try:
        raw = conn.execute(
            "SELECT captured_context FROM bug_reports WHERE app_id = ? "
            "ORDER BY created_at DESC LIMIT 1", (app_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    stored = json.loads(raw)
    assert stored["trace_id"] == TRACE_ID
    assert len(stored["recent_spans"]) == 2
    assert stored["recent_spans"][1]["error"] == "NOT NULL constraint failed"

    # And the analyzer prompt actually narrates them.
    from src.bug_reports.prompts import build_analyzer_user_prompt
    prompt = build_analyzer_user_prompt(
        bug_title="t", bug_description="d", captured_context=stored,
        files=[{"path": "src/App.tsx", "content": "x"}], version=None)
    assert "Traced events" in prompt
    assert "insert_order" in prompt and "NOT NULL constraint failed" in prompt


def test_writer_survives_successive_event_loops():
    """The module-global writer outlives event loops (every TestClient in a
    pytest process starts a fresh lifespan on a fresh loop). A queue bound to
    a dead loop used to hot-spin _run and freeze the new loop entirely."""
    from src.tracing.writer import SpanWriter

    w = SpanWriter()
    w.enqueue({"id": "pre-start"})  # before start: dropped, not raised
    assert w.dropped == 1

    row = {"id": "span-loop-2", "app_id": "loop-rebind-app", "purpose": "ai_toggle",
           "kind": "ai.call", "provider_type": "openai", "model": "m", "status": "ok",
           "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.0, "latency_ms": 1}

    async def first_loop():
        w.start()
        await w.stop()

    async def second_loop():
        w.start()  # must rebind to THIS loop, not the dead one
        w.enqueue(dict(row))
        await asyncio.wait_for(w.drain(), timeout=10)  # would hang pre-fix
        await w.stop()

    asyncio.run(first_loop())
    asyncio.run(second_loop())
    assert [r["id"] for r in _spans("loop-rebind-app")] == ["span-loop-2"]


def test_clear_trace(client, admin, plain_user):
    app_id = _widget_app(client, admin, "Clear Trace App")
    client.post(f"/api/apps/{app_id}/spans", json={"spans": [
        {"kind": "ui.error", "name": "boom", "status": "error"}]})
    _wait_for_spans(app_id, 1)

    assert client.delete(f"/api/apps/{app_id}/spans", headers=plain_user).status_code == 403
    r = client.delete(f"/api/apps/{app_id}/spans", headers=admin)
    assert r.status_code == 200 and r.json()["deleted"] >= 1
    assert _spans(app_id) == []
    assert client.delete("/api/apps/no-such/spans", headers=admin).status_code == 404


def test_spans_endpoint_rejects_negative_limit(client, admin):
    r = client.get("/api/apps/whatever/spans?limit=-1", headers=admin)
    assert r.status_code == 422  # ge=1 validation, before any DB work


def test_spans_endpoint_metadata_only_and_role_gated(client, admin, plain_user, provider, fake_llm):
    _set_capture_level("full")
    app_id = _toggle_app(client, admin, "Trace Endpoint App")
    client.post(f"/api/ai-toggle/{app_id}/chat", json={"message": "endpoint check"},
                headers={**admin, "X-AIHub-Trace-Id": TRACE_ID})
    _wait_for_spans(app_id, 1)

    r = client.get(f"/api/apps/{app_id}/spans", headers=admin)
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["purpose"] == "ai_toggle"
    assert row["has_prompt"] is True and row["has_response"] is True
    # Metadata only — no payloads (even encrypted) leave the DB via this route.
    assert "prompt_ct" not in row and "response_ct" not in row
    assert "endpoint check" not in json.dumps(rows)

    assert client.get(f"/api/apps/{app_id}/spans", headers=plain_user).status_code == 403
    assert client.get(f"/api/apps/{app_id}/spans").status_code in (401, 403)
    assert client.get("/api/apps/no-such-app/spans", headers=admin).status_code == 404
