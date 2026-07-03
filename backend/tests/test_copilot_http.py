"""Phase-4 D2 tests: the copilot diagnose endpoint (Suggest level).

Reuses the real analyzer pipeline; the LLM is faked at _acompletion_raw so
provider resolution, prompt assembly (traced events included), usage metering
under copilot_diagnose, and response mapping are all real.
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
_DB = _TMP / "test_copilot_http.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_copilot")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "copilot-test")

from src.config import settings  # noqa: E402
from src.database import init_db  # noqa: E402
from src.main import app as fastapi_app  # noqa: E402

SPANS = [
    {"kind": "ui.interaction", "name": "Save order", "status": "ok", "latency_ms": 0, "ts": 1},
    {"kind": "dataset.query", "name": "insert_order", "status": "error",
     "error": "NOT NULL constraint failed: orders.customer_id", "latency_ms": 240, "ts": 2},
]


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
def plain_user(client):
    return _login(client, "user")


@pytest.fixture(scope="module")
def provider(client, admin):
    p = client.post("/api/admin/ai-providers", json={
        "name": "Copilot Provider", "provider_type": "openai", "api_key": "ck",
        "default_model": "gpt-5.4-mini", "is_default_generation": True,
    }, headers=admin).json()
    yield p
    client.delete(f"/api/admin/ai-providers/{p['id']}", headers=admin)


@pytest.fixture()
def app_with_source(client, admin):
    app_id = client.post("/api/apps", json={"name": "Copilot App"}, headers=admin).json()["id"]
    src = Path(settings.app_data_dir) / app_id / "draft" / "frontend" / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "OrderForm.tsx").write_text("export const OrderForm = () => null\n", encoding="utf-8")
    return app_id


@pytest.fixture()
def fake_llm(monkeypatch):
    import src.llm_compat as llm_compat
    calls: list[dict] = []

    class _R:
        class _U:
            prompt_tokens, completion_tokens = 400, 80
        usage = _U()

        def __init__(self):
            self.choices = [type("C", (), {"message": type("M", (), {"content": json.dumps({
                "diagnosis": "The order form never sets customer_id before saving.",
                "root_cause": "OrderForm builds the insert params without the selected customer.",
                "risk_level": "low",
                "proposed_files": [{"path": "src/OrderForm.tsx", "action": "update", "content": "..."}],
            })})()})()]

    async def fake_raw(kwargs):
        calls.append(kwargs)
        return _R()
    monkeypatch.setattr(llm_compat, "_acompletion_raw", fake_raw)
    return calls


def _usage(app_id):
    conn = sqlite3.connect(settings.database_url[len("sqlite+aiosqlite:///"):])
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT purpose, user_id, error FROM llm_usage WHERE app_id = ?", (app_id,)).fetchall()]
    finally:
        conn.close()


def test_diagnose_returns_structured_result(client, admin, provider, app_with_source, fake_llm):
    from src import rate_limit
    rate_limit.copilot_limiter._buckets.clear()

    r = client.post(f"/api/copilot/{app_with_source}/diagnose", json={
        "issue_label": "insert_order failing: NOT NULL constraint failed",
        "trace_id": "copilot-trace-1",
        "spans": SPANS,
    }, headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "customer_id" in body["diagnosis"]
    assert body["risk_level"] == "low"
    assert body["files_implicated"] == [{"path": "src/OrderForm.tsx", "action": "update"}]
    # Suggest level: no fix content crosses this API.
    assert "content" not in json.dumps(body)

    # The analyzer prompt actually carried the traced chronology + source.
    prompt = fake_llm[0]["messages"][1]["content"]
    assert "Traced events" in prompt and "insert_order" in prompt
    assert "OrderForm.tsx" in prompt

    # Metered under the copilot purpose, attributed to the real developer.
    rows = _usage(app_with_source)
    assert len(rows) == 1
    assert rows[0]["purpose"] == "copilot_diagnose"
    assert rows[0]["user_id"] not in ("(system)", "(unknown)", None)


def test_diagnose_access_and_limits(client, admin, plain_user, provider, app_with_source, fake_llm):
    from src import rate_limit
    rate_limit.copilot_limiter._buckets.clear()

    body = {"issue_label": "x", "spans": []}
    # End users can't burn diagnosis tokens.
    assert client.post(f"/api/copilot/{app_with_source}/diagnose",
                       json=body, headers=plain_user).status_code == 403
    assert client.post("/api/copilot/no-such-app/diagnose",
                       json=body, headers=admin).status_code == 404
    assert client.post(f"/api/copilot/{app_with_source}/diagnose",
                       json=body).status_code in (401, 403)

    # Token bucket: burst of 5, then 429.
    ok = [client.post(f"/api/copilot/{app_with_source}/diagnose",
                      json=body, headers=admin).status_code for _ in range(6)]
    assert 429 in ok
    rate_limit.copilot_limiter._buckets.clear()
