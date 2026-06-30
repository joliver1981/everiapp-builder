"""TestClient tests for the cascade/orphan-prevention deletes:

  - DELETE /api/admin/connections/{id} → 409 if datasets reference it
  - DELETE /api/admin/datasets/{id}    → 204 and clears app_dataset_bindings

Caught by e2e_driver.py; regression-protected here.
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
    _TMP / "test_cascade.db",
    _AIHUB_TESTS_TMP / "test.db",
):
    if _candidate.exists():
        try:
            _candidate.unlink()
        except OSError:
            pass

_DB = _TMP / "test_cascade.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_cascade")
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


def _unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _make_connection(client: TestClient, admin_token: str) -> str:
    r = client.post(
        "/api/admin/connections",
        json={
            "name": _unique_name("cas"),
            "kind": "sql",
            "config": {"dialect": "sqlite", "database": ":memory:"},
        },
        headers=_auth(admin_token),
    )
    return r.json()["id"]


def _make_dataset(client: TestClient, admin_token: str, connection_id: str) -> str:
    r = client.post(
        "/api/admin/datasets",
        json={
            "name": _unique_name("cas-ds"),
            "connection_id": connection_id,
            "kind": "query",
            "definition": {"sql": "SELECT 1"},
        },
        headers=_auth(admin_token),
    )
    return r.json()["id"]


# --- Connection deletion guarded by referencing datasets -------------------


def test_delete_connection_with_datasets_returns_409(client: TestClient, admin_token: str):
    conn_id = _make_connection(client, admin_token)
    _make_dataset(client, admin_token, conn_id)

    r = client.delete(f"/api/admin/connections/{conn_id}", headers=_auth(admin_token))
    assert r.status_code == 409, r.text
    body = r.json()
    assert "still reference" in body["detail"]
    assert "Delete the datasets first" in body["detail"]


def test_delete_connection_409_mentions_dataset_names(client: TestClient, admin_token: str):
    conn_id = _make_connection(client, admin_token)
    name = _unique_name("findme")
    client.post(
        "/api/admin/datasets",
        json={
            "name": name,
            "connection_id": conn_id,
            "kind": "query",
            "definition": {"sql": "SELECT 1"},
        },
        headers=_auth(admin_token),
    )

    r = client.delete(f"/api/admin/connections/{conn_id}", headers=_auth(admin_token))
    assert r.status_code == 409
    assert name in r.json()["detail"]


def test_delete_connection_succeeds_after_datasets_removed(client: TestClient, admin_token: str):
    conn_id = _make_connection(client, admin_token)
    ds_id = _make_dataset(client, admin_token, conn_id)

    # First delete should fail
    r = client.delete(f"/api/admin/connections/{conn_id}", headers=_auth(admin_token))
    assert r.status_code == 409

    # Drop the dataset, retry
    r = client.delete(f"/api/admin/datasets/{ds_id}", headers=_auth(admin_token))
    assert r.status_code == 204

    r = client.delete(f"/api/admin/connections/{conn_id}", headers=_auth(admin_token))
    assert r.status_code == 204


# --- Dataset deletion cascades to bindings --------------------------------


def test_delete_dataset_clears_bindings(client: TestClient, admin_token: str):
    """Cascade: after delete_dataset, app_dataset_bindings has no rows
    pointing at the deleted dataset id."""
    conn_id = _make_connection(client, admin_token)
    ds_id = _make_dataset(client, admin_token, conn_id)

    # Insert an App + binding directly (apps service scaffolds files)
    from src.apps.models import App
    from src.database import async_session
    from src.datasets.models import AppDatasetBinding

    app_id = str(uuid.uuid4())

    async def _seed():
        async with async_session() as s:
            from ._helpers import fetch_admin_user_id_async
            creator = await fetch_admin_user_id_async(s)
            s.add(App(id=app_id, name=f"cas-{app_id[:8]}", description="", created_by=creator))
            await s.flush()
            s.add(AppDatasetBinding(app_id=app_id, dataset_id=ds_id))
            await s.commit()

    asyncio.run(_seed())

    # Confirm binding exists
    async def _binding_count() -> int:
        async with async_session() as s:
            r = await s.execute(
                select(AppDatasetBinding).where(AppDatasetBinding.dataset_id == ds_id)
            )
            return len(r.scalars().all())

    assert asyncio.run(_binding_count()) == 1

    # Delete dataset
    r = client.delete(f"/api/admin/datasets/{ds_id}", headers=_auth(admin_token))
    assert r.status_code == 204

    # Bindings should be gone
    assert asyncio.run(_binding_count()) == 0


def test_delete_dataset_audit_log_mentions_binding_count(client: TestClient, admin_token: str):
    """The delete audit row should note how many bindings were cleared."""
    conn_id = _make_connection(client, admin_token)
    ds_id = _make_dataset(client, admin_token, conn_id)

    from src.apps.models import App
    from src.database import async_session
    from src.datasets.models import AppDatasetBinding
    from src.secrets.models import AuditLog

    app_id = str(uuid.uuid4())

    async def _seed():
        async with async_session() as s:
            from ._helpers import fetch_admin_user_id_async
            creator = await fetch_admin_user_id_async(s)
            s.add(App(id=app_id, name=f"audit-{app_id[:8]}", description="", created_by=creator))
            await s.flush()
            s.add(AppDatasetBinding(app_id=app_id, dataset_id=ds_id))
            await s.commit()

    asyncio.run(_seed())

    r = client.delete(f"/api/admin/datasets/{ds_id}", headers=_auth(admin_token))
    assert r.status_code == 204

    async def _audit() -> str:
        async with async_session() as s:
            r = await s.execute(
                select(AuditLog).where(
                    AuditLog.resource_id == ds_id,
                    AuditLog.action == "dataset.delete",
                )
            )
            row = r.scalar_one()
            return row.details

    details = asyncio.run(_audit())
    assert "1 binding" in details
