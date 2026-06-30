"""TestClient integration tests for GET /api/admin/datasets/{id}/recent-calls."""
import asyncio
import os
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_AIHUB_TESTS_TMP = Path(tempfile.gettempdir()) / "aihub-tests"
for _candidate in (
    _TMP / "test_recent_calls.db",
    _AIHUB_TESTS_TMP / "test.db",
):
    if _candidate.exists():
        try:
            _candidate.unlink()
        except OSError:
            pass

_DB = _TMP / "test_recent_calls.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_recent")
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
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_sqlite(db_path: Path, ddl: list[str]) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text

    async def _run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        try:
            async with engine.begin() as c:
                for stmt in ddl:
                    await c.execute(text(stmt))
        finally:
            await engine.dispose()

    asyncio.run(_run())


def _insert_app_and_binding(app_id: str, dataset_id: str) -> None:
    from src.database import async_session
    from src.apps.models import App
    from src.datasets.models import AppDatasetBinding

    async def _run():
        async with async_session() as s:
            from ._helpers import fetch_admin_user_id_async
            creator = await fetch_admin_user_id_async(s)
            s.add(App(id=app_id, name=f"rc-{app_id[:8]}", description="", created_by=creator))
            await s.flush()
            s.add(AppDatasetBinding(app_id=app_id, dataset_id=dataset_id))
            await s.commit()

    asyncio.run(_run())


def test_recent_calls_returns_entries_after_executions(client: TestClient, admin_token: str):
    db_path = _TMP / f"rc-{uuid.uuid4().hex[:8]}.db"
    _seed_sqlite(db_path, ["CREATE TABLE t (id INTEGER)", "INSERT INTO t VALUES (1)"])

    r = client.post(
        "/api/admin/connections",
        json={
            "name": f"rc-{uuid.uuid4().hex[:8]}",
            "kind": "sql",
            "config": {"dialect": "sqlite", "database": str(db_path)},
        },
        headers=_auth(admin_token),
    )
    conn_id = r.json()["id"]

    r = client.post(
        "/api/admin/datasets",
        json={
            "name": f"rc-ds-{uuid.uuid4().hex[:8]}",
            "connection_id": conn_id,
            "kind": "query",
            "definition": {"sql": "SELECT id FROM t"},
        },
        headers=_auth(admin_token),
    )
    ds_id = r.json()["id"]
    app_id = str(uuid.uuid4())
    _insert_app_and_binding(app_id, ds_id)

    # Run twice
    for _ in range(2):
        ok = client.post(
            f"/api/apps/{app_id}/datasets/{ds_id}/execute",
            json={"params": {}},
            headers=_auth(admin_token),
        )
        assert ok.status_code == 200

    # Recent calls endpoint
    r = client.get(f"/api/admin/datasets/{ds_id}/recent-calls", headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["calls"]) >= 2
    actions = [c["action"] for c in body["calls"]]
    assert all(a in ("dataset.execute", "dataset.execute.error") for a in actions)
    # Newest first
    times = [c["created_at"] for c in body["calls"]]
    assert times == sorted(times, reverse=True)
    # Details include app id and row_count
    assert any(f"app={app_id}" in c["details"] for c in body["calls"])


def test_recent_calls_requires_admin(client: TestClient):
    r = client.get(f"/api/admin/datasets/{uuid.uuid4()}/recent-calls")
    assert r.status_code == 401


def test_recent_calls_empty_for_unused_dataset(client: TestClient, admin_token: str):
    # Make a dataset but never execute it
    r = client.post(
        "/api/admin/connections",
        json={
            "name": f"unused-{uuid.uuid4().hex[:8]}",
            "kind": "sql",
            "config": {"dialect": "sqlite", "database": ":memory:"},
        },
        headers=_auth(admin_token),
    )
    conn_id = r.json()["id"]
    r = client.post(
        "/api/admin/datasets",
        json={
            "name": f"unused-ds-{uuid.uuid4().hex[:8]}",
            "connection_id": conn_id,
            "kind": "query",
            "definition": {"sql": "SELECT 1"},
        },
        headers=_auth(admin_token),
    )
    ds_id = r.json()["id"]

    r = client.get(f"/api/admin/datasets/{ds_id}/recent-calls", headers=_auth(admin_token))
    assert r.status_code == 200
    assert r.json()["calls"] == []
