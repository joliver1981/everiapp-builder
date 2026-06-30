"""TestClient integration tests for the runtime dataset-execute endpoint
at /api/apps/{app_id}/datasets/{dataset_id}/execute.

Covers: success path with bound app, 403 on missing binding, current_user
injection, audit log entry, row limit enforcement, and rejection when params
fail.
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
    _TMP / "test_runtime.db",
    _AIHUB_TESTS_TMP / "test.db",
):
    if _candidate.exists():
        try:
            _candidate.unlink()
        except OSError:
            pass

_DB = _TMP / "test_runtime.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_runtime")
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
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text as _text

    async def _run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        try:
            async with engine.begin() as c:
                for stmt in ddl:
                    await c.execute(_text(stmt))
        finally:
            await engine.dispose()

    asyncio.run(_run())


def _create_sqlite_connection(client: TestClient, admin_token: str, db_path: Path) -> str:
    r = client.post(
        "/api/admin/connections",
        json={
            "name": _unique_name("rt-sqlite"),
            "kind": "sql",
            "config": {"dialect": "sqlite", "database": str(db_path)},
        },
        headers=_auth(admin_token),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _create_dataset(client: TestClient, admin_token: str, conn_id: str, sql: str, name: str | None = None) -> str:
    r = client.post(
        "/api/admin/datasets",
        json={
            "name": name or _unique_name("ds"),
            "connection_id": conn_id,
            "kind": "query",
            "definition": {"sql": sql},
        },
        headers=_auth(admin_token),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _insert_app_and_binding(app_id: str, dataset_id: str) -> None:
    """Bypass the apps service (which scaffolds files on disk) and insert
    the rows directly. Runtime executor only cares that both rows exist.
    """
    from src.database import async_session
    from src.apps.models import App
    from src.datasets.models import AppDatasetBinding

    async def _run():
        async with async_session() as s:
            from ._helpers import fetch_admin_user_id_async
            creator = await fetch_admin_user_id_async(s)
            s.add(App(id=app_id, name=f"runtime-test-{app_id[:8]}", description="", created_by=creator))
            await s.flush()
            s.add(AppDatasetBinding(app_id=app_id, dataset_id=dataset_id))
            await s.commit()

    asyncio.run(_run())


# --- Success path ----------------------------------------------------------


def test_execute_returns_rows_for_bound_app(client: TestClient, admin_token: str):
    db_path = _TMP / f"ok-{uuid.uuid4().hex[:8]}.db"
    _seed_sqlite(db_path, [
        "CREATE TABLE items (id INTEGER, label TEXT)",
        "INSERT INTO items VALUES (1, 'a')",
        "INSERT INTO items VALUES (2, 'b')",
    ])
    conn_id = _create_sqlite_connection(client, admin_token, db_path)
    ds_id = _create_dataset(client, admin_token, conn_id, "SELECT id, label FROM items ORDER BY id")
    app_id = str(uuid.uuid4())
    _insert_app_and_binding(app_id, ds_id)

    r = client.post(
        f"/api/apps/{app_id}/datasets/{ds_id}/execute",
        json={"params": {}},
        headers=_auth(admin_token),
    )
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["row_count"] == 2
    assert result["truncated"] is False
    assert result["rows"][0]["label"] == "a"


def test_execute_403_without_binding(client: TestClient, admin_token: str):
    db_path = _TMP / f"nobind-{uuid.uuid4().hex[:8]}.db"
    _seed_sqlite(db_path, ["CREATE TABLE t (id INTEGER)"])
    conn_id = _create_sqlite_connection(client, admin_token, db_path)
    ds_id = _create_dataset(client, admin_token, conn_id, "SELECT id FROM t")
    # No binding inserted

    r = client.post(
        f"/api/apps/{uuid.uuid4()}/datasets/{ds_id}/execute",
        json={"params": {}},
        headers=_auth(admin_token),
    )
    assert r.status_code == 403, r.text
    assert "not bound" in r.json()["detail"]


# NOTE: A previous test (`test_execute_404_for_missing_dataset`) used to check
# what happened when a binding pointed at a dataset_id that didn't exist. That
# state is now unreachable: the platform DB has foreign_keys=ON, so SQLite
# refuses to insert an app_dataset_bindings row whose dataset_id isn't in
# `datasets`. The cascade-delete-on-dataset-delete path (covered in
# test_cascade_http.py) makes orphan bindings impossible.


def test_execute_injects_current_user(client: TestClient, admin_token: str):
    """A dataset that selects :current_user should receive the caller's username."""
    db_path = _TMP / f"user-{uuid.uuid4().hex[:8]}.db"
    _seed_sqlite(db_path, ["CREATE TABLE t (id INTEGER)"])
    conn_id = _create_sqlite_connection(client, admin_token, db_path)
    ds_id = _create_dataset(client, admin_token, conn_id, "SELECT :current_user AS who")
    app_id = str(uuid.uuid4())
    _insert_app_and_binding(app_id, ds_id)

    r = client.post(
        f"/api/apps/{app_id}/datasets/{ds_id}/execute",
        json={"params": {}},
        headers=_auth(admin_token),
    )
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["row_count"] == 1
    assert result["rows"][0]["who"] == "admin"


def test_execute_writes_audit_log(client: TestClient, admin_token: str):
    db_path = _TMP / f"audit-{uuid.uuid4().hex[:8]}.db"
    _seed_sqlite(db_path, ["CREATE TABLE t (id INTEGER)", "INSERT INTO t VALUES (1)"])
    conn_id = _create_sqlite_connection(client, admin_token, db_path)
    ds_id = _create_dataset(client, admin_token, conn_id, "SELECT id FROM t")
    app_id = str(uuid.uuid4())
    _insert_app_and_binding(app_id, ds_id)

    r = client.post(
        f"/api/apps/{app_id}/datasets/{ds_id}/execute",
        json={"params": {}},
        headers=_auth(admin_token),
    )
    assert r.status_code == 200

    async def _check():
        from src.database import async_session
        from src.secrets.models import AuditLog

        async with async_session() as s:
            result = await s.execute(
                select(AuditLog).where(AuditLog.resource_id == ds_id)
            )
            return [(r.action, r.details) for r in result.scalars().all()]

    rows = asyncio.run(_check())
    actions = [a for a, _ in rows]
    assert "dataset.execute" in actions
    # Audit detail should mention the app and row count
    exec_detail = next(d for a, d in rows if a == "dataset.execute")
    assert f"app={app_id}" in exec_detail
    assert "row_count=1" in exec_detail


def test_execute_enforces_row_limit_override(client: TestClient, admin_token: str):
    """Set a tiny per-dataset row_limit_override and confirm `truncated` flips."""
    db_path = _TMP / f"limit-{uuid.uuid4().hex[:8]}.db"
    seeds = ["CREATE TABLE big (id INTEGER)"]
    seeds.extend([f"INSERT INTO big VALUES ({i})" for i in range(10)])
    _seed_sqlite(db_path, seeds)
    conn_id = _create_sqlite_connection(client, admin_token, db_path)

    r = client.post(
        "/api/admin/datasets",
        json={
            "name": _unique_name("capped"),
            "connection_id": conn_id,
            "kind": "query",
            "definition": {"sql": "SELECT id FROM big"},
            "row_limit_override": 3,
        },
        headers=_auth(admin_token),
    )
    assert r.status_code == 201, r.text
    ds_id = r.json()["id"]
    app_id = str(uuid.uuid4())
    _insert_app_and_binding(app_id, ds_id)

    r = client.post(
        f"/api/apps/{app_id}/datasets/{ds_id}/execute",
        json={"params": {}},
        headers=_auth(admin_token),
    )
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["row_count"] == 3
    assert result["truncated"] is True


def test_execute_requires_auth(client: TestClient):
    r = client.post(
        f"/api/apps/{uuid.uuid4()}/datasets/{uuid.uuid4()}/execute",
        json={"params": {}},
    )
    assert r.status_code == 401
