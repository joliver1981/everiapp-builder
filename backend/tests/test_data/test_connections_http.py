"""TestClient integration tests for /api/admin/connections.

Per the CLAUDE.md rule: every HTTP route is exercised via the real FastAPI
request pipeline, including auth and audit logging. Includes an end-to-end
test_connection() against an in-memory SQLite to prove the driver dispatch works.
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
    _TMP / "test_connections.db",
    _AIHUB_TESTS_TMP / "test.db",
):
    if _candidate.exists():
        try:
            _candidate.unlink()
        except OSError:
            pass

_DB = _TMP / "test_connections.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_connections")
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


def test_create_list_get_connection_via_http(client: TestClient, admin_token: str):
    name = _unique_name("test-sqlite")
    body = {
        "name": name,
        "description": "test sqlite connection",
        "kind": "sql",
        "config": {"dialect": "sqlite", "database": ":memory:"},
    }
    r = client.post("/api/admin/connections", json=body, headers=_auth(admin_token))
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["name"] == name
    assert created["kind"] == "sql"
    assert created["default_row_limit"] == 500000  # default applied
    assert created["read_only"] is True
    conn_id = created["id"]

    # List
    r = client.get("/api/admin/connections", headers=_auth(admin_token))
    assert r.status_code == 200
    names = [c["name"] for c in r.json()]
    assert name in names

    # Get one
    r = client.get(f"/api/admin/connections/{conn_id}", headers=_auth(admin_token))
    assert r.status_code == 200
    assert r.json()["id"] == conn_id


def test_update_connection_via_http(client: TestClient, admin_token: str):
    body = {
        "name": _unique_name("test-upd"),
        "kind": "sql",
        "config": {"dialect": "sqlite", "database": ":memory:"},
    }
    r = client.post("/api/admin/connections", json=body, headers=_auth(admin_token))
    assert r.status_code == 201
    conn_id = r.json()["id"]

    r = client.put(
        f"/api/admin/connections/{conn_id}",
        json={"description": "updated", "default_row_limit": 1000},
        headers=_auth(admin_token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["description"] == "updated"
    assert r.json()["default_row_limit"] == 1000


def test_delete_connection_via_http(client: TestClient, admin_token: str):
    body = {
        "name": _unique_name("test-del"),
        "kind": "sql",
        "config": {"dialect": "sqlite", "database": ":memory:"},
    }
    r = client.post("/api/admin/connections", json=body, headers=_auth(admin_token))
    conn_id = r.json()["id"]

    r = client.delete(f"/api/admin/connections/{conn_id}", headers=_auth(admin_token))
    assert r.status_code == 204

    r = client.get(f"/api/admin/connections/{conn_id}", headers=_auth(admin_token))
    assert r.status_code == 404


def test_test_connection_runs_select_1(client: TestClient, admin_token: str):
    """End-to-end: real sqlite connection, real SELECT 1."""
    body = {
        "name": _unique_name("test-conn-live"),
        "kind": "sql",
        "config": {"dialect": "sqlite", "database": ":memory:"},
    }
    r = client.post("/api/admin/connections", json=body, headers=_auth(admin_token))
    conn_id = r.json()["id"]

    r = client.post(f"/api/admin/connections/{conn_id}/test", headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["success"] is True, result
    assert result["response_time_ms"] is not None


def test_test_connection_reports_failure_for_bad_dialect(client: TestClient, admin_token: str):
    body = {
        "name": _unique_name("test-bad-dialect"),
        "kind": "sql",
        "config": {"dialect": "not-a-real-dialect"},
    }
    r = client.post("/api/admin/connections", json=body, headers=_auth(admin_token))
    conn_id = r.json()["id"]

    r = client.post(f"/api/admin/connections/{conn_id}/test", headers=_auth(admin_token))
    assert r.status_code == 200
    result = r.json()
    assert result["success"] is False
    assert "Unknown SQL dialect" in result["message"]


def test_test_connection_writes_audit_log(client: TestClient, admin_token: str):
    body = {
        "name": _unique_name("test-audit"),
        "kind": "sql",
        "config": {"dialect": "sqlite", "database": ":memory:"},
    }
    r = client.post("/api/admin/connections", json=body, headers=_auth(admin_token))
    conn_id = r.json()["id"]

    # Trigger test_connection
    r = client.post(f"/api/admin/connections/{conn_id}/test", headers=_auth(admin_token))
    assert r.status_code == 200

    # Verify audit log entries exist for create + test
    async def _check():
        from src.database import async_session
        from src.secrets.models import AuditLog

        async with async_session() as s:
            result = await s.execute(
                select(AuditLog).where(AuditLog.resource_id == conn_id).order_by(AuditLog.created_at)
            )
            return [r.action for r in result.scalars().all()]

    actions = asyncio.run(_check())
    assert "connection.create" in actions
    assert "connection.test" in actions


def test_create_requires_admin(client: TestClient):
    # No auth header
    r = client.post(
        "/api/admin/connections",
        json={"name": "x", "kind": "sql", "config": {}},
    )
    assert r.status_code == 401


def test_duplicate_name_rejected(client: TestClient, admin_token: str):
    name = _unique_name("dupe")
    body = {"name": name, "kind": "sql", "config": {"dialect": "sqlite", "database": ":memory:"}}
    r = client.post("/api/admin/connections", json=body, headers=_auth(admin_token))
    assert r.status_code == 201

    r = client.post("/api/admin/connections", json=body, headers=_auth(admin_token))
    assert r.status_code == 400
    assert "already exists" in r.json()["detail"]


def test_create_rest_connection_via_http(client: TestClient, admin_token: str):
    body = {
        "name": _unique_name("test-rest"),
        "kind": "rest",
        "config": {
            "base_url": "https://example.com",
            "auth_type": "bearer",
            "default_headers": {"Accept": "application/json"},
        },
    }
    r = client.post("/api/admin/connections", json=body, headers=_auth(admin_token))
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "rest"
    assert r.json()["config"]["auth_type"] == "bearer"
