"""TestClient integration tests for App↔Dataset bindings and the
developer-facing /api/datasets/discoverable endpoint.
"""
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
    _TMP / "test_bindings.db",
    _AIHUB_TESTS_TMP / "test.db",
):
    if _candidate.exists():
        try:
            _candidate.unlink()
        except OSError:
            pass

_DB = _TMP / "test_bindings.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_bindings")
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
def developer_token(client: TestClient) -> str:
    r = client.post("/api/auth/login", json={"username": "developer", "password": "password"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _make_connection_and_dataset(client: TestClient, admin_token: str, visibility: str = "org") -> tuple[str, str]:
    """Create a sqlite connection and a query dataset; return (connection_id, dataset_id)."""
    r = client.post(
        "/api/admin/connections",
        json={
            "name": _unique_name("bind-sqlite"),
            "kind": "sql",
            "config": {"dialect": "sqlite", "database": ":memory:"},
        },
        headers=_auth(admin_token),
    )
    assert r.status_code == 201, r.text
    conn_id = r.json()["id"]

    r = client.post(
        "/api/admin/datasets",
        json={
            "name": _unique_name("bind-ds"),
            "connection_id": conn_id,
            "kind": "query",
            "definition": {"sql": "SELECT 1 AS one"},
            "visibility": visibility,
        },
        headers=_auth(admin_token),
    )
    assert r.status_code == 201, r.text
    return conn_id, r.json()["id"]


def _insert_app(name_prefix: str = "bound") -> str:
    """Bypass the apps service (no file scaffolding); insert App row directly."""
    from src.database import async_session
    from src.apps.models import App

    app_id = str(uuid.uuid4())

    async def _run():
        async with async_session() as s:
            from ._helpers import fetch_admin_user_id_async
            creator = await fetch_admin_user_id_async(s)
            s.add(App(id=app_id, name=f"{name_prefix}-{app_id[:8]}", description="", created_by=creator))
            await s.commit()

    asyncio.run(_run())
    return app_id


# --- Bindings --------------------------------------------------------------


def test_bind_and_list(client: TestClient, admin_token: str):
    _, ds_id = _make_connection_and_dataset(client, admin_token)
    app_id = _insert_app()

    # Bind
    r = client.post(f"/api/apps/{app_id}/datasets/{ds_id}", headers=_auth(admin_token))
    assert r.status_code == 201, r.text
    assert r.json()["id"] == ds_id

    # List
    r = client.get(f"/api/apps/{app_id}/datasets", headers=_auth(admin_token))
    assert r.status_code == 200
    ids = [d["id"] for d in r.json()]
    assert ds_id in ids


def test_bind_is_idempotent(client: TestClient, admin_token: str):
    _, ds_id = _make_connection_and_dataset(client, admin_token)
    app_id = _insert_app()

    r1 = client.post(f"/api/apps/{app_id}/datasets/{ds_id}", headers=_auth(admin_token))
    assert r1.status_code == 201
    r2 = client.post(f"/api/apps/{app_id}/datasets/{ds_id}", headers=_auth(admin_token))
    assert r2.status_code == 201  # same result, no error

    # Listing still shows it exactly once
    r = client.get(f"/api/apps/{app_id}/datasets", headers=_auth(admin_token))
    matches = [d for d in r.json() if d["id"] == ds_id]
    assert len(matches) == 1


def test_unbind(client: TestClient, admin_token: str):
    _, ds_id = _make_connection_and_dataset(client, admin_token)
    app_id = _insert_app()

    client.post(f"/api/apps/{app_id}/datasets/{ds_id}", headers=_auth(admin_token))
    r = client.delete(f"/api/apps/{app_id}/datasets/{ds_id}", headers=_auth(admin_token))
    assert r.status_code == 204

    # Listing no longer includes it
    r = client.get(f"/api/apps/{app_id}/datasets", headers=_auth(admin_token))
    ids = [d["id"] for d in r.json()]
    assert ds_id not in ids


def test_unbind_404_when_not_bound(client: TestClient, admin_token: str):
    _, ds_id = _make_connection_and_dataset(client, admin_token)
    app_id = _insert_app()

    r = client.delete(f"/api/apps/{app_id}/datasets/{ds_id}", headers=_auth(admin_token))
    assert r.status_code == 404


def test_bind_unknown_dataset_returns_404(client: TestClient, admin_token: str):
    app_id = _insert_app()
    r = client.post(
        f"/api/apps/{app_id}/datasets/{uuid.uuid4()}",
        headers=_auth(admin_token),
    )
    assert r.status_code == 404


def test_bindings_require_auth(client: TestClient):
    r = client.get(f"/api/apps/{uuid.uuid4()}/datasets")
    assert r.status_code == 401


# --- Discoverable ---------------------------------------------------------


def test_discoverable_lists_org_datasets_for_developer(client: TestClient, admin_token: str, developer_token: str):
    _, org_ds = _make_connection_and_dataset(client, admin_token, visibility="org")

    r = client.get("/api/datasets/discoverable", headers=_auth(developer_token))
    assert r.status_code == 200, r.text
    ids = [d["id"] for d in r.json()]
    assert org_ds in ids


def test_discoverable_hides_others_private(client: TestClient, admin_token: str, developer_token: str):
    # Admin creates a private dataset
    _, private_ds = _make_connection_and_dataset(client, admin_token, visibility="private")

    # Developer (different user) should not see it
    r = client.get("/api/datasets/discoverable", headers=_auth(developer_token))
    assert r.status_code == 200
    ids = [d["id"] for d in r.json()]
    assert private_ds not in ids


def test_discoverable_shows_own_private(client: TestClient, admin_token: str):
    # Admin creates a private dataset → admin can see it
    _, private_ds = _make_connection_and_dataset(client, admin_token, visibility="private")

    r = client.get("/api/datasets/discoverable", headers=_auth(admin_token))
    assert r.status_code == 200
    ids = [d["id"] for d in r.json()]
    assert private_ds in ids
