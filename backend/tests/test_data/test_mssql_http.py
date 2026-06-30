"""MSSQL regression tests — guard the cross-dialect row-cap fix.

These only run when pyodbc/aioodbc are installed AND a local SQL Server is
reachable via Windows auth at `localhost` with the database AIHUB_TEST_MSSQL_DB
(default `LLMDB`). The CI/dev gate will skip them gracefully if either is
missing.

Why these exist: the original implementation wrapped user SQL as
`SELECT * FROM (sql) _x LIMIT N` to enforce row caps. That works on
sqlite/postgres/mysql but breaks on MSSQL (no LIMIT keyword, ORDER BY in
derived tables requires TOP/OFFSET). The driver switched to fetchmany(N+1)
which is dialect-agnostic — these tests lock that in.
"""
import asyncio
import os
import tempfile
import uuid
from pathlib import Path

import pytest

pyodbc = pytest.importorskip("pyodbc")
pytest.importorskip("aioodbc")

MSSQL_DB = os.environ.get("AIHUB_TEST_MSSQL_DB", "LLMDB")
MSSQL_DRIVER = os.environ.get("AIHUB_TEST_MSSQL_DRIVER", "ODBC Driver 17 for SQL Server")
MSSQL_HOST = os.environ.get("AIHUB_TEST_MSSQL_HOST", "localhost")


def _mssql_reachable() -> bool:
    try:
        cs = (
            f"DRIVER={{{MSSQL_DRIVER}}};SERVER={MSSQL_HOST};"
            f"DATABASE={MSSQL_DB};Trusted_Connection=yes"
        )
        conn = pyodbc.connect(cs, timeout=3)
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _mssql_reachable(),
    reason=f"MSSQL not reachable (host={MSSQL_HOST}, db={MSSQL_DB})",
)


# --- Test fixture setup --------------------------------------------------

from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_AIHUB_TESTS_TMP = Path(tempfile.gettempdir()) / "aihub-tests"
for _candidate in (
    _TMP / "test_mssql.db",
    _AIHUB_TESTS_TMP / "test.db",
):
    if _candidate.exists():
        try:
            _candidate.unlink()
        except OSError:
            pass

_DB = _TMP / "test_mssql.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_mssql")
os.environ["DEBUG"] = "true"
os.environ.setdefault(
    "MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8="
)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

from src.database import init_db  # noqa: E402
from src.main import app  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _init_db():
    asyncio.run(init_db())
    yield


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin_token(client: TestClient) -> str:
    r = client.post("/api/auth/login", json={"username": "admin", "password": "password"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def mssql_connection_id(client: TestClient, admin_token: str) -> str:
    r = client.post(
        "/api/admin/connections",
        json={
            "name": f"mssql-test-{uuid.uuid4().hex[:6]}",
            "kind": "sql",
            "config": {
                "dialect": "mssql",
                "host": MSSQL_HOST,
                "database": MSSQL_DB,
                "extra_params": {
                    "driver": MSSQL_DRIVER,
                    "Trusted_Connection": "yes",
                },
            },
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --- Tests ----------------------------------------------------------------


def test_mssql_test_connection(client: TestClient, admin_token: str, mssql_connection_id: str):
    r = client.post(
        f"/api/admin/connections/{mssql_connection_id}/test",
        headers=_auth(admin_token),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"], body
    assert body["response_time_ms"] is not None


def test_mssql_introspect_schemas_includes_dbo(client: TestClient, admin_token: str, mssql_connection_id: str):
    r = client.get(
        f"/api/admin/connections/{mssql_connection_id}/schema",
        headers=_auth(admin_token),
    )
    assert r.status_code == 200, r.text
    assert "dbo" in r.json()["schemas"]


def test_mssql_preview_query_with_order_by(client: TestClient, admin_token: str, mssql_connection_id: str):
    """The wrapping bug: an unwrapped query with ORDER BY but no TOP/OFFSET
    would have been wrapped as `SELECT * FROM (... ORDER BY name) _preview LIMIT N`,
    which MSSQL rejects. With fetchmany(N+1) there's no wrapping, so this works.
    """
    r = client.post(
        "/api/admin/datasets/preview",
        json={
            "connection_id": mssql_connection_id,
            "kind": "query",
            "definition": {
                "sql": "SELECT name, type_desc FROM sys.objects WHERE type IN ('U','V') ORDER BY name"
            },
            "params": {},
        },
        headers=_auth(admin_token),
    )
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["row_count"] >= 0
    assert isinstance(res["truncated"], bool)


def test_mssql_preview_named_param(client: TestClient, admin_token: str, mssql_connection_id: str):
    """Named params (`:kind`) get bound through SQLAlchemy text() — same as sqlite."""
    r = client.post(
        "/api/admin/datasets/preview",
        json={
            "connection_id": mssql_connection_id,
            "kind": "query",
            "definition": {"sql": "SELECT name FROM sys.objects WHERE type = :k ORDER BY name"},
            "params": {"k": "U"},
        },
        headers=_auth(admin_token),
    )
    assert r.status_code == 200, r.text
    res = r.json()
    # zero rows is fine — the test is that the query parsed and bound the param
    assert "rows" in res


def test_mssql_dataset_save_infers_output_schema(client: TestClient, admin_token: str, mssql_connection_id: str):
    """LIMIT 0 wrapping previously failed silently → output_schema={} on MSSQL.
    Now using execute-then-close-without-iterating, we get column metadata.
    """
    r = client.post(
        "/api/admin/datasets",
        json={
            "name": f"mssql-shape-{uuid.uuid4().hex[:6]}",
            "connection_id": mssql_connection_id,
            "kind": "query",
            "definition": {"sql": "SELECT name, object_id FROM sys.objects"},
        },
        headers=_auth(admin_token),
    )
    assert r.status_code == 201, r.text
    output = r.json()["output_schema"]
    assert output.get("type") == "array"
    props = output.get("items", {}).get("properties", {})
    assert "name" in props
    assert "object_id" in props


def test_mssql_runtime_execute_with_top_and_order_by(client: TestClient, admin_token: str, mssql_connection_id: str):
    """End-to-end runtime path on MSSQL: bind dataset, execute, get rows back.
    Previously failed with `Incorrect syntax near 'LIMIT'` because runtime wrapped.
    """
    from src.apps.models import App
    from src.database import async_session
    from src.datasets.models import AppDatasetBinding

    # Create dataset
    ds_resp = client.post(
        "/api/admin/datasets",
        json={
            "name": f"mssql-rt-{uuid.uuid4().hex[:6]}",
            "connection_id": mssql_connection_id,
            "kind": "query",
            "definition": {
                "sql": "SELECT TOP 3 name, type_desc FROM sys.objects "
                       "WHERE type IN ('U','V') ORDER BY name"
            },
        },
        headers=_auth(admin_token),
    )
    assert ds_resp.status_code == 201, ds_resp.text
    ds_id = ds_resp.json()["id"]

    # Create App + binding directly (apps service scaffolds files on disk)
    app_id = str(uuid.uuid4())

    async def _seed():
        async with async_session() as s:
            from ._helpers import fetch_admin_user_id_async
            creator = await fetch_admin_user_id_async(s)
            s.add(App(id=app_id, name=f"mssql-rt-{app_id[:8]}", description="", created_by=creator))
            await s.flush()
            s.add(AppDatasetBinding(app_id=app_id, dataset_id=ds_id))
            await s.commit()

    asyncio.run(_seed())

    r = client.post(
        f"/api/apps/{app_id}/datasets/{ds_id}/execute",
        json={"params": {}},
        headers=_auth(admin_token),
    )
    assert r.status_code == 200, r.text
    res = r.json()
    # We asked for TOP 3 — should get at most 3 rows
    assert 0 <= res["row_count"] <= 3
    assert isinstance(res["truncated"], bool)
