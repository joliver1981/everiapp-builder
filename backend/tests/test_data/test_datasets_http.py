"""TestClient integration tests for /api/admin/datasets + the schema-
introspection route on /api/admin/connections.
"""
import asyncio
import os
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_AIHUB_TESTS_TMP = Path(tempfile.gettempdir()) / "aihub-tests"
for _candidate in (
    _TMP / "test_datasets.db",
    _AIHUB_TESTS_TMP / "test.db",
):
    if _candidate.exists():
        try:
            _candidate.unlink()
        except OSError:
            pass

_DB = _TMP / "test_datasets.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_datasets")
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


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _seed_sqlite(db_path: Path, ddl: list[str]) -> None:
    """Run DDL/DML against a real on-disk sqlite file via a fresh aiosqlite engine.
    Preview opens its own engine, so we can't use :memory: — must persist to a path.
    """
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text as _text

    async def _run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        try:
            async with engine.begin() as conn:
                for stmt in ddl:
                    await conn.execute(_text(stmt))
        finally:
            await engine.dispose()

    asyncio.run(_run())


def _create_sqlite_connection(client: TestClient, admin_token: str, db_path: Path) -> str:
    body = {
        "name": _unique_name("test-sqlite"),
        "kind": "sql",
        "config": {"dialect": "sqlite", "database": str(db_path)},
    }
    r = client.post("/api/admin/connections", json=body, headers=_auth(admin_token))
    assert r.status_code == 201, r.text
    return r.json()["id"]


# --- CRUD ------------------------------------------------------------------


def test_create_list_get_dataset_via_http(client: TestClient, admin_token: str):
    db_path = _TMP / f"crud-{uuid.uuid4().hex[:8]}.db"
    _seed_sqlite(db_path, ["CREATE TABLE orders (id INTEGER, total REAL)"])
    conn_id = _create_sqlite_connection(client, admin_token, db_path)

    body = {
        "name": _unique_name("orders"),
        "connection_id": conn_id,
        "kind": "query",
        "definition": {"sql": "SELECT id, total FROM orders"},
        "parameter_schema": {"type": "object", "properties": {}},
    }
    r = client.post("/api/admin/datasets", json=body, headers=_auth(admin_token))
    assert r.status_code == 201, r.text
    ds = r.json()
    assert ds["kind"] == "query"
    assert ds["visibility"] == "private"
    # output_schema should have been inferred on save
    assert ds["output_schema"].get("type") == "array"

    # List
    r = client.get("/api/admin/datasets", headers=_auth(admin_token))
    assert r.status_code == 200
    ids = [d["id"] for d in r.json()]
    assert ds["id"] in ids

    # Get
    r = client.get(f"/api/admin/datasets/{ds['id']}", headers=_auth(admin_token))
    assert r.status_code == 200
    assert r.json()["id"] == ds["id"]


def test_update_dataset_via_http(client: TestClient, admin_token: str):
    db_path = _TMP / f"upd-{uuid.uuid4().hex[:8]}.db"
    _seed_sqlite(db_path, ["CREATE TABLE t (id INTEGER)"])
    conn_id = _create_sqlite_connection(client, admin_token, db_path)

    r = client.post(
        "/api/admin/datasets",
        json={
            "name": _unique_name("upd"),
            "connection_id": conn_id,
            "kind": "query",
            "definition": {"sql": "SELECT id FROM t"},
        },
        headers=_auth(admin_token),
    )
    assert r.status_code == 201, r.text
    ds_id = r.json()["id"]

    r = client.put(
        f"/api/admin/datasets/{ds_id}",
        json={"description": "updated", "visibility": "org"},
        headers=_auth(admin_token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["description"] == "updated"
    assert r.json()["visibility"] == "org"


def test_delete_dataset_via_http(client: TestClient, admin_token: str):
    db_path = _TMP / f"del-{uuid.uuid4().hex[:8]}.db"
    _seed_sqlite(db_path, ["CREATE TABLE t (id INTEGER)"])
    conn_id = _create_sqlite_connection(client, admin_token, db_path)

    r = client.post(
        "/api/admin/datasets",
        json={
            "name": _unique_name("del"),
            "connection_id": conn_id,
            "kind": "query",
            "definition": {"sql": "SELECT id FROM t"},
        },
        headers=_auth(admin_token),
    )
    ds_id = r.json()["id"]

    r = client.delete(f"/api/admin/datasets/{ds_id}", headers=_auth(admin_token))
    assert r.status_code == 204

    r = client.get(f"/api/admin/datasets/{ds_id}", headers=_auth(admin_token))
    assert r.status_code == 404


# --- Preview ---------------------------------------------------------------


def test_preview_sql_query(client: TestClient, admin_token: str):
    db_path = _TMP / f"prev-{uuid.uuid4().hex[:8]}.db"
    _seed_sqlite(db_path, [
        "CREATE TABLE customers (id INTEGER, name TEXT)",
        "INSERT INTO customers VALUES (1, 'Alice')",
        "INSERT INTO customers VALUES (2, 'Bob')",
        "INSERT INTO customers VALUES (3, 'Carol')",
    ])
    conn_id = _create_sqlite_connection(client, admin_token, db_path)

    r = client.post(
        "/api/admin/datasets/preview",
        json={
            "connection_id": conn_id,
            "kind": "query",
            "definition": {"sql": "SELECT id, name FROM customers ORDER BY id"},
            "params": {},
        },
        headers=_auth(admin_token),
    )
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["row_count"] == 3
    assert result["truncated"] is False
    assert {c["name"] for c in result["columns"]} == {"id", "name"}
    assert result["rows"][0]["name"] == "Alice"


def test_preview_enforces_row_cap(client: TestClient, admin_token: str):
    db_path = _TMP / f"cap-{uuid.uuid4().hex[:8]}.db"
    seeds = ["CREATE TABLE big (id INTEGER)"]
    seeds.extend([f"INSERT INTO big VALUES ({i})" for i in range(150)])
    _seed_sqlite(db_path, seeds)
    conn_id = _create_sqlite_connection(client, admin_token, db_path)

    r = client.post(
        "/api/admin/datasets/preview",
        json={
            "connection_id": conn_id,
            "kind": "query",
            "definition": {"sql": "SELECT id FROM big"},
        },
        headers=_auth(admin_token),
    )
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["row_count"] == 100
    assert result["truncated"] is True


def test_preview_table_kind(client: TestClient, admin_token: str):
    db_path = _TMP / f"tab-{uuid.uuid4().hex[:8]}.db"
    _seed_sqlite(db_path, [
        "CREATE TABLE widgets (id INTEGER, label TEXT)",
        "INSERT INTO widgets VALUES (1, 'a')",
        "INSERT INTO widgets VALUES (2, 'b')",
    ])
    conn_id = _create_sqlite_connection(client, admin_token, db_path)

    r = client.post(
        "/api/admin/datasets/preview",
        json={
            "connection_id": conn_id,
            "kind": "table",
            "definition": {"schema": "main", "table_name": "widgets", "column_allowlist": ["id", "label"]},
        },
        headers=_auth(admin_token),
    )
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["row_count"] == 2
    assert {c["name"] for c in result["columns"]} == {"id", "label"}


# --- Introspection ---------------------------------------------------------


def test_schema_introspection_lists_tables(client: TestClient, admin_token: str):
    db_path = _TMP / f"intro-{uuid.uuid4().hex[:8]}.db"
    _seed_sqlite(db_path, [
        "CREATE TABLE alpha (id INTEGER)",
        "CREATE TABLE beta (id INTEGER, name TEXT)",
    ])
    conn_id = _create_sqlite_connection(client, admin_token, db_path)

    # schemas-only
    r = client.get(f"/api/admin/connections/{conn_id}/schema", headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    assert r.json()["schemas"] == ["main"]

    # tables in schema
    r = client.get(
        f"/api/admin/connections/{conn_id}/schema",
        params={"schema": "main"},
        headers=_auth(admin_token),
    )
    assert r.status_code == 200, r.text
    tables = r.json()["tables"]
    assert "alpha" in tables and "beta" in tables

    # columns in table
    r = client.get(
        f"/api/admin/connections/{conn_id}/schema",
        params={"schema": "main", "table": "beta"},
        headers=_auth(admin_token),
    )
    assert r.status_code == 200, r.text
    col_names = [c["name"] for c in r.json()["columns"]]
    assert col_names == ["id", "name"]


# --- Guards ----------------------------------------------------------------


def test_dataset_create_requires_admin(client: TestClient):
    r = client.post(
        "/api/admin/datasets",
        json={"name": "x", "connection_id": "x", "kind": "query", "definition": {"sql": "SELECT 1"}},
    )
    assert r.status_code == 401


def test_dataset_create_writes_audit_log(client: TestClient, admin_token: str):
    db_path = _TMP / f"aud-{uuid.uuid4().hex[:8]}.db"
    _seed_sqlite(db_path, ["CREATE TABLE t (id INTEGER)"])
    conn_id = _create_sqlite_connection(client, admin_token, db_path)

    r = client.post(
        "/api/admin/datasets",
        json={
            "name": _unique_name("aud"),
            "connection_id": conn_id,
            "kind": "query",
            "definition": {"sql": "SELECT id FROM t"},
        },
        headers=_auth(admin_token),
    )
    assert r.status_code == 201
    ds_id = r.json()["id"]

    async def _check():
        from src.database import async_session
        from src.secrets.models import AuditLog
        async with async_session() as s:
            result = await s.execute(
                select(AuditLog).where(AuditLog.resource_id == ds_id).order_by(AuditLog.created_at)
            )
            return [r.action for r in result.scalars().all()]

    actions = asyncio.run(_check())
    assert "dataset.create" in actions


def test_output_schema_inferred_for_query_with_named_params(client: TestClient, admin_token: str):
    """Regression: previously, _best_effort_infer_output passed {} as params
    so any :name placeholder caused a 'bound parameter not provided' error
    and we silently returned output_schema={}. Now we stub all declared
    params with NULL so introspection succeeds."""
    db_path = _TMP / f"named-{uuid.uuid4().hex[:8]}.db"
    _seed_sqlite(db_path, ["CREATE TABLE t (id INTEGER, name TEXT)"])
    conn_id = _create_sqlite_connection(client, admin_token, db_path)

    r = client.post(
        "/api/admin/datasets",
        json={
            "name": _unique_name("named-params"),
            "connection_id": conn_id,
            "kind": "query",
            "definition": {"sql": "SELECT id, name FROM t WHERE id = :which"},
            "parameter_schema": {
                "type": "object",
                "properties": {"which": {"type": "integer"}},
                "required": ["which"],
            },
        },
        headers=_auth(admin_token),
    )
    assert r.status_code == 201, r.text
    ds = r.json()
    assert ds["output_schema"].get("type") == "array", (
        f"output_schema not inferred — got {ds['output_schema']}"
    )
    props = ds["output_schema"]["items"]["properties"]
    assert set(props.keys()) == {"id", "name"}


def test_create_dataset_for_unknown_connection_returns_400(client: TestClient, admin_token: str):
    r = client.post(
        "/api/admin/datasets",
        json={
            "name": _unique_name("bad-conn"),
            "connection_id": "00000000-0000-0000-0000-000000000000",
            "kind": "query",
            "definition": {"sql": "SELECT 1"},
        },
        headers=_auth(admin_token),
    )
    assert r.status_code == 400
    assert "not found" in r.json()["detail"]
