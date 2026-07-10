"""Phase 2: app server functions (callFunction) — Python files under
server/functions/ executed in a child interpreter on the platform host.

Real HTTP routes (TestClient, real auth), and a REAL child Python process for
invoke tests (dev branch of python_env → sys.executable, which is this test
run's interpreter). Tests whose function uses ctx.* need the platform reachable
over a real socket (the child dials back with urllib), so those run against a
uvicorn-in-thread on an ephemeral port; everything else uses plain TestClient —
a function that never touches ctx never dials back.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import textwrap
import threading
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_server_functions.db"
if _DB.exists():
    _DB.unlink()
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_DB}")
os.environ.setdefault("APP_DATA_DIR", str(_TMP / "apps_server_functions"))
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "server-functions-test")

from src.auth.service import auth_service  # noqa: E402
from src.config import settings  # noqa: E402
from src.database import init_db  # noqa: E402
from src.main import app as fastapi_app  # noqa: E402
import src.connections.drivers.rest as rest_driver  # noqa: E402
from src.rate_limit import fn_limiter  # noqa: E402

# In a FULL pytest run other modules assign settings.app_data_dir at IMPORT
# time (e.g. tests/test_versions/test_publish_http.py) — and collection
# imports every module before any test runs, so the alphabetically-LAST
# assignment is what the backend uses at runtime. Never capture these paths
# at import; read the live settings at test time.
def _apps_dir() -> Path:
    return Path(settings.app_data_dir)


def _db_path() -> Path:
    url = settings.database_url
    return Path(url.split("///", 1)[1]) if "///" in url else _DB


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
def live_server(client):
    """The same FastAPI app on a real socket, for functions that call ctx.*
    (the child process dials http://127.0.0.1:{port} back into the platform).
    Same process → same DB engine, env, and rest_driver module (so a
    monkeypatched build_client is visible to requests served here)."""
    import uvicorn

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(
        fastapi_app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn test server failed to start"
    yield port
    server.should_exit = True
    t.join(timeout=5)


def _make_app(client, admin) -> str:
    r = client.post("/api/apps", json={"name": f"fn-{uuid.uuid4().hex[:6]}"}, headers=admin)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _write_fn(app_id: str, name: str, code: str) -> Path:
    fdir = _apps_dir() / app_id / "draft" / "frontend" / "server" / "functions"
    fdir.mkdir(parents=True, exist_ok=True)
    path = fdir / f"{name}.py"
    path.write_text(textwrap.dedent(code), encoding="utf-8")
    return path


def _scoped_headers(admin, app_id, purpose="preview"):
    """Mint what the runtime proxy injects into a running app as
    window.__AIHUB_TOKEN__ — a purpose-scoped token bound to this app."""
    raw = admin["Authorization"].split(" ", 1)[1]
    payload = auth_service.decode_access_token(raw)
    tok = auth_service.create_access_token(
        payload["sub"], payload["role"], expire_minutes=60,
        extra_claims={"purpose": purpose, "app_id": app_id,
                      "username": payload.get("username", "")},
    )
    return {"Authorization": f"Bearer {tok}"}


# --- fake upstream at the rest-driver seam (same shape as external-calls) ---
class _FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self.headers = {"content-type": "application/json"}
        self.encoding = "utf-8"
        self._body = json.dumps(payload if payload is not None else {"ok": True}).encode()

    async def aiter_bytes(self):
        yield self._body


class _FakeStream:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class _FakeClient:
    def __init__(self, sink):
        self._sink = sink

    def stream(self, method, path, **kwargs):
        self._sink["method"] = method
        self._sink["path"] = path
        self._sink["kwargs"] = kwargs
        return _FakeStream(_FakeResp(payload={"echo": path}))

    async def aclose(self):
        pass


@pytest.fixture()
def captured(monkeypatch):
    sink: dict = {}

    def fake_build_client(config, *, secret=None, timeout_seconds=30):
        sink["config"] = config
        sink["secret"] = secret
        return _FakeClient(sink)

    monkeypatch.setattr(rest_driver, "build_client", fake_build_client)
    return sink


# ---------------------------------------------------------------------------
# List + happy path
# ---------------------------------------------------------------------------

def test_scaffold_includes_server_sdk(client, admin):
    app_id = _make_app(client, admin)
    assert (_apps_dir() / app_id / "draft" / "frontend" / "server" / "sdk.py").is_file()


def test_list_and_invoke_happy_path(client, admin):
    app_id = _make_app(client, admin)
    _write_fn(app_id, "echo", """
        def handler(args, ctx):
            return {"doubled": args["n"] * 2}
    """)

    r = client.get(f"/api/apps/{app_id}/fn", headers=admin)
    assert r.status_code == 200, r.text
    assert r.json() == [{"name": "echo", "runtime": "python", "timeout_s": 30}]

    r = client.post(f"/api/apps/{app_id}/fn/echo", json={"args": {"n": 21}}, headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["result"] == {"doubled": 42}
    assert isinstance(body["duration_ms"], int)


def test_scoped_token_invokes_and_cross_app_is_denied(client, admin):
    app_id = _make_app(client, admin)
    _write_fn(app_id, "whoami", """
        def handler(args, ctx):
            return {"user": ctx.user["username"], "app": ctx.app_id}
    """)

    r = client.post(f"/api/apps/{app_id}/fn/whoami", json={"args": None},
                    headers=_scoped_headers(admin, app_id))
    assert r.status_code == 200, r.text
    assert r.json()["result"] == {"user": "admin", "app": app_id}

    # A token minted for a DIFFERENT app can't invoke this app's functions.
    r = client.post(f"/api/apps/{app_id}/fn/whoami", json={"args": None},
                    headers=_scoped_headers(admin, "some-other-app"))
    assert r.status_code == 403


def test_unknown_function_is_a_fixable_404(client, admin):
    app_id = _make_app(client, admin)
    r = client.post(f"/api/apps/{app_id}/fn/nope", json={"args": None}, headers=admin)
    assert r.status_code == 404
    assert "server/functions/" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Failure surface
# ---------------------------------------------------------------------------

def test_function_exception_is_a_400_with_the_message(client, admin):
    app_id = _make_app(client, admin)
    _write_fn(app_id, "boom", """
        def handler(args, ctx):
            raise ValueError("boom")
    """)
    r = client.post(f"/api/apps/{app_id}/fn/boom", json={"args": None}, headers=admin)
    assert r.status_code == 400
    assert "boom" in r.json()["detail"]


def test_missing_handler_is_a_400_with_the_fix(client, admin):
    app_id = _make_app(client, admin)
    _write_fn(app_id, "nohandler", """
        VALUE = 1
    """)
    r = client.post(f"/api/apps/{app_id}/fn/nohandler", json={"args": None}, headers=admin)
    assert r.status_code == 400
    assert "def handler(args, ctx):" in r.json()["detail"]


def test_non_json_result_is_a_400_with_conversion_advice(client, admin):
    app_id = _make_app(client, admin)
    _write_fn(app_id, "badjson", """
        def handler(args, ctx):
            return {1, 2, 3}
    """)
    r = client.post(f"/api/apps/{app_id}/fn/badjson", json={"args": None}, headers=admin)
    assert r.status_code == 400
    assert "JSON-serializable" in r.json()["detail"]


@pytest.mark.slow
def test_busy_loop_is_killed_at_the_timeout(client, admin):
    app_id = _make_app(client, admin)
    _write_fn(app_id, "spin", """
        CONFIG = {"timeout_s": 2}

        def handler(args, ctx):
            while True:
                pass
    """)
    t0 = time.monotonic()
    r = client.post(f"/api/apps/{app_id}/fn/spin", json={"args": None}, headers=admin)
    assert r.status_code == 504
    assert "timeout" in r.json()["detail"].lower()
    assert time.monotonic() - t0 < 15  # 2s config + 5s grace + slack


def test_print_output_lands_in_logs_and_never_corrupts_the_envelope(client, admin):
    app_id = _make_app(client, admin)
    _write_fn(app_id, "chatty", """
        def handler(args, ctx):
            print("hello from the function")
            ctx.log("via ctx.log")
            return 1
    """)
    r = client.post(f"/api/apps/{app_id}/fn/chatty", json={"args": None}, headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"] == 1
    joined = "\n".join(body["logs"])
    assert "hello from the function" in joined
    assert "via ctx.log" in joined


def test_curated_library_import_works(client, admin):
    pytest.importorskip("pandas")
    app_id = _make_app(client, admin)
    _write_fn(app_id, "stats", """
        import pandas as pd

        def handler(args, ctx):
            df = pd.DataFrame({"x": args["values"]})
            return {"total": float(df["x"].sum())}
    """)
    r = client.post(f"/api/apps/{app_id}/fn/stats",
                    json={"args": {"values": [1, 2, 3.5]}}, headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["result"] == {"total": 6.5}


# ---------------------------------------------------------------------------
# ctx bridge — needs the platform on a real socket
# ---------------------------------------------------------------------------

def test_ctx_db_roundtrip_with_limit_override(client, admin, live_server):
    import httpx

    app_id = _make_app(client, admin)
    _write_fn(app_id, "tally", """
        def handler(args, ctx):
            ctx.db.exec("CREATE TABLE IF NOT EXISTS t (x INTEGER)")
            for i in range(args["n"]):
                ctx.db.exec("INSERT INTO t (x) VALUES (:x)", {"x": i})
            r = ctx.db.query("SELECT COUNT(*) AS n FROM t", limit=2000)
            return {"count": r["rows"][0]["n"], "truncated": r["truncated"]}
    """)
    r = httpx.post(f"http://127.0.0.1:{live_server}/api/apps/{app_id}/fn/tally",
                   json={"args": {"n": 5}}, headers=admin, timeout=60)
    assert r.status_code == 200, r.text
    assert r.json()["result"] == {"count": 5, "truncated": False}


def test_ctx_call_connection_through_the_real_route(client, admin, live_server, captured):
    import httpx

    app_id = _make_app(client, admin)
    cr = client.post("/api/admin/connections", json={
        "name": f"conn-{uuid.uuid4().hex[:6]}", "kind": "rest",
        "config": {"base_url": "https://api.example.com", "auth_type": "bearer"},
        "app_callable": True,
    }, headers=admin)
    conn_id = cr.json()["id"]
    assert client.post(f"/api/apps/{app_id}/connections/{conn_id}",
                       headers=admin).status_code in (200, 201)

    _write_fn(app_id, "fetchit", """
        def handler(args, ctx):
            res = ctx.call_connection(args["conn"], method="POST", path="/v1/data",
                                      body={"q": 1})
            return res["body"]
    """)
    r = httpx.post(f"http://127.0.0.1:{live_server}/api/apps/{app_id}/fn/fetchit",
                   json={"args": {"conn": conn_id}}, headers=admin, timeout=60)
    assert r.status_code == 200, r.text
    assert r.json()["result"] == {"echo": "/v1/data"}
    # The call went through the real connection route (gates + fake upstream).
    assert captured["method"] == "POST"
    assert captured["path"] == "/v1/data"


def test_ctx_errors_carry_the_platform_detail(client, admin, live_server):
    """An unbound connection's fixable 403 message must surface verbatim in the
    function's error — that's what lets the AI/user fix it without guessing."""
    import httpx

    app_id = _make_app(client, admin)
    cr = client.post("/api/admin/connections", json={
        "name": f"unbound-{uuid.uuid4().hex[:6]}", "kind": "rest",
        "config": {"base_url": "https://api.example.com", "auth_type": "none"},
        "app_callable": True,
    }, headers=admin)  # app-callable but NOT attached to this app

    _write_fn(app_id, "sneaky", """
        def handler(args, ctx):
            return ctx.call_connection(args["conn"], method="GET", path="/x")
    """)
    r = httpx.post(f"http://127.0.0.1:{live_server}/api/apps/{app_id}/fn/sneaky",
                   json={"args": {"conn": cr.json()["id"]}}, headers=admin, timeout=60)
    assert r.status_code == 400
    assert "attach" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Guards, versioning, rate limit
# ---------------------------------------------------------------------------

def test_generation_write_guard_blocks_server_sdk(client, admin):
    from src.ai.code_parser import GeneratedFile
    from src.ai.service import ai_service

    app_id = _make_app(client, admin)
    sdk_path = _apps_dir() / app_id / "draft" / "frontend" / "server" / "sdk.py"
    original = sdk_path.read_text(encoding="utf-8")

    asyncio.run(ai_service._save_generated_files(app_id, [
        GeneratedFile(path="server/sdk.py", content="# hacked", action="create"),
        GeneratedFile(path="server/functions/legit.py",
                      content="def handler(args, ctx):\n    return 1\n", action="create"),
    ]))
    assert sdk_path.read_text(encoding="utf-8") == original          # blocked
    assert (sdk_path.parent / "functions" / "legit.py").is_file()    # allowed

    from src.bug_reports.analyzer import is_platform_owned_path
    assert is_platform_owned_path("server/sdk.py")
    assert is_platform_owned_path("server//SDK.py")  # canonicalized spellings too
    assert not is_platform_owned_path("server/functions/mine.py")


def test_resolve_fn_dir_is_absolute_even_with_relative_app_data_dir(monkeypatch):
    """In dev, settings.app_data_dir is RELATIVE ("./data/apps"); the child
    process runs with cwd=source_dir, so a relative fn path handed to it
    re-resolves against that cwd (live bug: doubled path, FileNotFoundError).
    resolve_fn_dir must return absolute paths regardless of the setting."""
    from src.functions.service import resolve_fn_dir

    class _App:
        id = "some-app"
        current_version = 0

    monkeypatch.setattr(settings, "app_data_dir", "./data/apps")
    source_dir, source = resolve_fn_dir(_App(), {"purpose": "preview"})
    assert source == "draft"
    assert source_dir.is_absolute()


def test_python_file_blocks_parse_with_hash_file_header():
    from src.ai.code_parser import parse_llm_response

    files, _desc, _wiz = parse_llm_response(
        "Here you go:\n\n```python\n# FILE: server/functions/calc.py\n"
        "def handler(args, ctx):\n    return 1\n```\n"
    )
    assert [f.path for f in files] == ["server/functions/calc.py"]
    assert "def handler" in files[0].content


def test_published_version_is_immutable_for_embed_callers(client, admin):
    app_id = _make_app(client, admin)
    _write_fn(app_id, "which", """
        def handler(args, ctx):
            return "draft-v1"
    """)
    r = client.post(f"/api/apps/{app_id}/versions", json={"notes": "v1"}, headers=admin)
    assert r.status_code == 201, r.text
    assert (_apps_dir() / app_id / "versions" / "v1" / "server" / "functions" / "which.py").is_file()

    # Draft moves on; the published version must not.
    _write_fn(app_id, "which", """
        def handler(args, ctx):
            return "draft-v2"
    """)

    preview = client.post(f"/api/apps/{app_id}/fn/which", json={"args": None},
                          headers=_scoped_headers(admin, app_id, purpose="preview"))
    embed = client.post(f"/api/apps/{app_id}/fn/which", json={"args": None},
                        headers=_scoped_headers(admin, app_id, purpose="embed"))
    assert preview.json()["result"] == "draft-v2"
    assert embed.json()["result"] == "draft-v1"


def test_draft_only_function_tells_embed_callers_to_publish(client, admin):
    app_id = _make_app(client, admin)
    _write_fn(app_id, "early", """
        def handler(args, ctx):
            return 1
    """)
    client.post(f"/api/apps/{app_id}/versions", json={"notes": "v1"}, headers=admin)
    _write_fn(app_id, "newer", """
        def handler(args, ctx):
            return 2
    """)
    r = client.post(f"/api/apps/{app_id}/fn/newer", json={"args": None},
                    headers=_scoped_headers(admin, app_id, purpose="embed"))
    assert r.status_code == 404
    assert "publish" in r.json()["detail"].lower()


def test_admin_installed_package_is_importable_by_a_real_child(client, admin, monkeypatch):
    """Phase 2.1 wiring, end to end with a REAL interpreter: a module placed in
    the managed packages dir (as an admin install would) is importable by a
    server function via meta.extra_sys_path — no platform restart involved."""
    import src.python_env as python_env

    managed = _TMP / f"managed-{uuid.uuid4().hex[:6]}" / "server-packages"
    managed.mkdir(parents=True)
    (managed / "mymod.py").write_text("VALUE = 41\n", encoding="utf-8")
    dist = managed / "mymod-1.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: mymod\nVersion: 1.0\n")
    monkeypatch.setattr(python_env, "managed_packages_dir", lambda: managed)

    app_id = _make_app(client, admin)
    _write_fn(app_id, "uses-pkg", """
        import mymod

        def handler(args, ctx):
            return {"v": mymod.VALUE + 1}
    """)
    r = client.post(f"/api/apps/{app_id}/fn/uses-pkg", json={"args": None}, headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["result"] == {"v": 42}


def test_missing_import_error_names_the_admin_page(client, admin):
    app_id = _make_app(client, admin)
    _write_fn(app_id, "needslib", """
        import definitely_not_installed_lib

        def handler(args, ctx):
            return 1
    """)
    r = client.post(f"/api/apps/{app_id}/fn/needslib", json={"args": None}, headers=admin)
    assert r.status_code == 400
    assert "Admin → Python Packages" in r.json()["detail"]


def test_rate_limit_kicks_in(client, admin):
    app_id = _make_app(client, admin)
    fn_limiter.reset(app_id)
    # The limiter gate sits BEFORE name resolution, so a missing name still
    # consumes a token — a retry storm can't spawn interpreters.
    statuses = [
        client.post(f"/api/apps/{app_id}/fn/missing", json={"args": None},
                    headers=admin).status_code
        for _ in range(40)
    ]
    assert statuses[0] == 404
    assert 429 in statuses
    fn_limiter.reset(app_id)


def test_invoke_writes_an_audit_row(client, admin):
    app_id = _make_app(client, admin)
    _write_fn(app_id, "audited", """
        def handler(args, ctx):
            return "ok"
    """)
    r = client.post(f"/api/apps/{app_id}/fn/audited", json={"args": None}, headers=admin)
    assert r.status_code == 200

    import sqlite3
    con = sqlite3.connect(_db_path())
    try:
        n = con.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action = 'app_function.call' "
            "AND resource_id = ?", (f"{app_id}/audited",)).fetchone()[0]
    finally:
        con.close()
    assert n == 1
