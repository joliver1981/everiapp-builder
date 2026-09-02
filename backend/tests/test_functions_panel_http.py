"""Functions panel — the developer-facing routes behind the builder's
Functions tab, through the real HTTP layer with a REAL child interpreter for
the test runs (same setup as test_server_functions_http.py).

    GET  /api/apps/{id}/functions               list + summary + callers + published presence
    POST /api/apps/{id}/functions               scaffold
    POST /api/apps/{id}/functions/{name}/run    test run (draft), errors as 200 {ok:false}
    GET  /api/apps/{id}/functions/calls         call log (panel + app triggers)
    GET  /api/apps/{id}/functions/{name}/calls
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import textwrap
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_functions_panel.db"
if _DB.exists():
    _DB.unlink()
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_DB}")
os.environ.setdefault("APP_DATA_DIR", str(_TMP / "apps_functions_panel"))
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "functions-panel-test")

from src.auth.service import auth_service  # noqa: E402
from src.config import settings  # noqa: E402
from src.database import init_db  # noqa: E402
from src.main import app as fastapi_app  # noqa: E402


def _apps_dir() -> Path:
    # Read live — another module may reassign settings.app_data_dir at import.
    return Path(settings.app_data_dir)


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


def _make_app(client, admin) -> str:
    r = client.post("/api/apps", json={"name": f"panel-{uuid.uuid4().hex[:6]}"}, headers=admin)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _write(app_id: str, rel: str, code: str) -> Path:
    path = _apps_dir() / app_id / "draft" / "frontend" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(code), encoding="utf-8")
    return path


def _scoped_headers(admin, app_id, purpose="preview"):
    raw = admin["Authorization"].split(" ", 1)[1]
    payload = auth_service.decode_access_token(raw)
    tok = auth_service.create_access_token(
        payload["sub"], payload["role"], expire_minutes=60,
        extra_claims={"purpose": purpose, "app_id": app_id,
                      "username": payload.get("username", "")},
    )
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture(scope="module")
def app_with_functions(client, admin):
    app_id = _make_app(client, admin)
    _write(app_id, "server/functions/add.py", '''
        """Add two numbers.

        Longer description that must not appear in the summary.
        """
        CONFIG = {"timeout_s": 45}

        def handler(args, ctx):
            ctx.log("adding", args)
            return {"sum": args["a"] + args["b"]}
    ''')
    _write(app_id, "server/functions/boom.py", '''
        def handler(args, ctx):
            """Always fails — for the panel's error display."""
            print("about to fail")
            raise ValueError("nope")
    ''')
    # Private helper (underscore prefix) — never listed as a function.
    _write(app_id, "server/functions/_shared.py", "X = 1\n")
    # A UI call site the panel cross-references, plus a call to a function
    # that doesn't exist (dangling) which the list must not invent.
    _write(app_id, "src/pages/Totals.tsx", '''
        import { callFunction } from '../sdk'
        export async function total() {
          const r = await callFunction('add', { a: 1, b: 2 })
          const g = await callFunction("gone", {})
          return [r, g]
        }
    ''')
    # The hook form, with the multi-line generic generated code tends to write.
    _write(app_id, "src/components/Danger.tsx", '''
        import { useFunction } from '@aihub/app-sdk'
        export function Danger() {
          const boom = useFunction<{ ok: boolean
            error?: string }>('boom')
          const typed = useFunction<AddResult>('add')
          return null
        }
    ''')
    return app_id


def test_list_describes_functions(client, admin, app_with_functions):
    app_id = app_with_functions
    r = client.get(f"/api/apps/{app_id}/functions", headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["runtime_available"] is True
    assert body["published_version"] == 0
    by_name = {f["name"]: f for f in body["functions"]}
    assert set(by_name) == {"add", "boom"}  # _shared.py is a helper, not a function

    add = by_name["add"]
    assert add["runtime"] == "python"
    assert add["path"] == "server/functions/add.py"
    assert add["timeout_s"] == 45
    assert add["summary"] == "Add two numbers."
    # callFunction('add') in one file, useFunction<AddResult>('add') in another.
    assert add["callers"] == ["src/components/Danger.tsx", "src/pages/Totals.tsx"]
    assert add["in_published"] is False
    assert add["size_bytes"] > 0 and add["modified_at"].endswith("Z")

    boom = by_name["boom"]
    assert boom["summary"] == "Always fails — for the panel's error display."
    assert boom["timeout_s"] == 30  # default
    # Found through the hook with a multi-line generic.
    assert boom["callers"] == ["src/components/Danger.tsx"]


def test_published_presence_tracks_versions(client, admin, app_with_functions):
    app_id = app_with_functions
    r = client.post(f"/api/apps/{app_id}/versions", json={"notes": "v1"}, headers=admin)
    assert r.status_code == 201, r.text
    # A function added after the snapshot is draft-only until the next version.
    _write(app_id, "server/functions/later.py", "def handler(args, ctx):\n    return 1\n")
    r = client.get(f"/api/apps/{app_id}/functions", headers=admin)
    by_name = {f["name"]: f for f in r.json()["functions"]}
    assert r.json()["published_version"] == 1
    assert by_name["add"]["in_published"] is True
    assert by_name["later"]["in_published"] is False


def test_scaffold_new_function(client, admin, app_with_functions):
    app_id = app_with_functions
    r = client.post(f"/api/apps/{app_id}/functions", json={"name": "summarize-orders"}, headers=admin)
    assert r.status_code == 201, r.text
    assert r.json() == {"name": "summarize-orders", "path": "server/functions/summarize-orders.py"}
    text = (_apps_dir() / app_id / "draft" / "frontend" / "server" / "functions"
            / "summarize-orders.py").read_text(encoding="utf-8")
    assert "def handler(args, ctx: Ctx):" in text
    assert "callFunction('summarize-orders', args)" in text
    assert 'CONFIG = {"timeout_s": 30}' in text

    # The scaffold runs as-is: it echoes its args.
    r = client.post(f"/api/apps/{app_id}/functions/summarize-orders/run",
                    json={"args": {"hello": "world"}}, headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert r.json()["result"] == {"ok": True, "echo": {"hello": "world"}}

    # Duplicate and bad names are refused with fixable messages.
    r = client.post(f"/api/apps/{app_id}/functions", json={"name": "summarize-orders"}, headers=admin)
    assert r.status_code == 409
    r = client.post(f"/api/apps/{app_id}/functions", json={"name": "Bad Name!"}, headers=admin)
    assert r.status_code == 400
    assert "lowercase" in r.json()["detail"]


def test_run_returns_result_and_logs(client, admin, app_with_functions):
    app_id = app_with_functions
    r = client.post(f"/api/apps/{app_id}/functions/add/run",
                    json={"args": {"a": 2, "b": 3}}, headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["result"] == {"sum": 5}
    assert any("adding" in line for line in body["logs"])
    assert body["duration_ms"] >= 0


def test_run_reports_function_errors_as_results(client, admin, app_with_functions):
    """A failing function is something the developer wants to READ, not an
    HTTP error: 200 with ok:false, the message, and what it printed."""
    app_id = app_with_functions
    r = client.post(f"/api/apps/{app_id}/functions/boom/run", json={"args": None}, headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "ValueError: nope" in body["error"]
    assert any("about to fail" in line for line in body["logs"])
    assert body["status_code"] == 400


def test_run_unknown_function_is_404(client, admin, app_with_functions):
    r = client.post(f"/api/apps/{app_with_functions}/functions/nope/run", json={"args": {}}, headers=admin)
    assert r.status_code == 404
    assert "no server function named 'nope'" in r.json()["detail"]


def test_call_log_records_panel_and_app_triggers(client, admin, app_with_functions):
    app_id = app_with_functions
    # The running app invokes through the SDK route with its scoped token.
    r = client.post(f"/api/apps/{app_id}/fn/add", json={"args": {"a": 10, "b": 5}},
                    headers=_scoped_headers(admin, app_id))
    assert r.status_code == 200, r.text

    r = client.get(f"/api/apps/{app_id}/functions/calls", headers=admin)
    assert r.status_code == 200, r.text
    calls = r.json()["calls"]
    assert calls[0]["fn_name"] == "add" and calls[0]["trigger"] == "app"
    assert calls[0]["ok"] is True and calls[0]["source"] == "draft"
    assert calls[0]["args_preview"] == '{"a": 10, "b": 5}'
    assert calls[0]["result_preview"] == '{"sum": 15}'
    assert "adding" in calls[0]["logs"]

    triggers = {(c["fn_name"], c["trigger"], c["ok"]) for c in calls}
    assert ("add", "panel", True) in triggers
    assert ("boom", "panel", False) in triggers
    failed = next(c for c in calls if c["fn_name"] == "boom")
    assert "ValueError: nope" in failed["error"] and failed["status_code"] == 400
    assert failed["result_preview"] == ""

    r = client.get(f"/api/apps/{app_id}/functions/boom/calls", headers=admin)
    assert {c["fn_name"] for c in r.json()["calls"]} == {"boom"}


def test_panel_routes_require_developer_login(client, admin, app_with_functions):
    """A running app's scoped token can invoke functions, but it must not get
    the developer surface (source listing, scaffolding, test runs)."""
    app_id = app_with_functions
    scoped = _scoped_headers(admin, app_id)
    assert client.get(f"/api/apps/{app_id}/functions", headers=scoped).status_code in (401, 403)
    assert client.post(f"/api/apps/{app_id}/functions", json={"name": "x"}, headers=scoped).status_code in (401, 403)
    assert client.post(f"/api/apps/{app_id}/functions/add/run", json={"args": {}}, headers=scoped).status_code in (401, 403)
    assert client.get(f"/api/apps/{app_id}/functions").status_code in (401, 403)


def test_missing_app_is_404(client, admin):
    r = client.get("/api/apps/does-not-exist/functions", headers=admin)
    assert r.status_code == 404
