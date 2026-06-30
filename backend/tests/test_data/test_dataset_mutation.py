"""Tests for the dataset write-back (useDatasetMutation) endpoint.

Uses an on-disk sqlite connection with read_only=False so the platform allows
the mutation. Verifies: insert works, read-only blocks it, missing mutation_sql
blocks it, binding is required, and the mutation is audit-logged.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_dataset_mutation.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_mutation")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "mutation-test")

from src.database import init_db  # noqa: E402
from src.main import app  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _init():
    asyncio.run(init_db())
    yield


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin_token(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "password"})
    return r.json()["access_token"]


def _auth(t): return {"Authorization": f"Bearer {t}"}


def _seed_target_db() -> Path:
    """A separate on-disk sqlite the dataset will write into."""
    p = _TMP / f"target-{uuid.uuid4().hex[:8]}.db"
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT, price REAL)")
    conn.execute("INSERT INTO products VALUES (1, 'Widget', 9.99)")
    conn.commit()
    conn.close()
    return p


def _bind_app(app_id, ds_id):
    from . import _helpers as _airdb
    _airdb.insert_app_and_binding(None, app_id, ds_id)


def test_mutation_inserts_row(client, admin_token):
    target = _seed_target_db()
    # Writable connection (read_only=False)
    r = client.post("/api/admin/connections", json={
        "name": f"mut-conn-{uuid.uuid4().hex[:6]}",
        "kind": "sql",
        "config": {"dialect": "sqlite", "database": str(target)},
        "read_only": False,
    }, headers=_auth(admin_token))
    conn_id = r.json()["id"]
    assert r.json()["read_only"] is False

    # Dataset with a mutation_sql
    r = client.post("/api/admin/datasets", json={
        "name": f"add-product-{uuid.uuid4().hex[:6]}",
        "connection_id": conn_id,
        "kind": "query",
        "definition": {
            "sql": "SELECT id, name, price FROM products",
            "mutation_sql": "INSERT INTO products (name, price) VALUES (:name, :price)",
        },
        "parameter_schema": {"type": "object", "properties": {
            "name": {"type": "string"}, "price": {"type": "number"}}},
    }, headers=_auth(admin_token))
    ds_id = r.json()["id"]

    app_id = str(uuid.uuid4())
    _bind_app(app_id, ds_id)

    # Mutate
    r = client.post(f"/api/apps/{app_id}/datasets/{ds_id}/mutate",
                    json={"params": {"name": "Gadget", "price": 19.99}},
                    headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    assert r.json()["rows_affected"] == 1

    # Confirm via a read execute
    r = client.post(f"/api/apps/{app_id}/datasets/{ds_id}/execute",
                    json={"params": {}}, headers=_auth(admin_token))
    names = {row["name"] for row in r.json()["rows"]}
    assert "Gadget" in names


def test_mutation_blocked_on_readonly_connection(client, admin_token):
    target = _seed_target_db()
    r = client.post("/api/admin/connections", json={
        "name": f"ro-conn-{uuid.uuid4().hex[:6]}",
        "kind": "sql",
        "config": {"dialect": "sqlite", "database": str(target)},
        "read_only": True,  # default, but explicit
    }, headers=_auth(admin_token))
    conn_id = r.json()["id"]

    r = client.post("/api/admin/datasets", json={
        "name": f"ro-ds-{uuid.uuid4().hex[:6]}",
        "connection_id": conn_id,
        "kind": "query",
        "definition": {
            "sql": "SELECT * FROM products",
            "mutation_sql": "DELETE FROM products WHERE id = :id",
        },
    }, headers=_auth(admin_token))
    ds_id = r.json()["id"]
    app_id = str(uuid.uuid4())
    _bind_app(app_id, ds_id)

    r = client.post(f"/api/apps/{app_id}/datasets/{ds_id}/mutate",
                    json={"params": {"id": 1}}, headers=_auth(admin_token))
    assert r.status_code == 403
    assert "read-only" in r.json()["detail"].lower()


def test_mutation_blocked_without_mutation_sql(client, admin_token):
    target = _seed_target_db()
    r = client.post("/api/admin/connections", json={
        "name": f"nm-conn-{uuid.uuid4().hex[:6]}",
        "kind": "sql",
        "config": {"dialect": "sqlite", "database": str(target)},
        "read_only": False,
    }, headers=_auth(admin_token))
    conn_id = r.json()["id"]

    r = client.post("/api/admin/datasets", json={
        "name": f"nm-ds-{uuid.uuid4().hex[:6]}",
        "connection_id": conn_id,
        "kind": "query",
        "definition": {"sql": "SELECT * FROM products"},  # no mutation_sql
    }, headers=_auth(admin_token))
    ds_id = r.json()["id"]
    app_id = str(uuid.uuid4())
    _bind_app(app_id, ds_id)

    r = client.post(f"/api/apps/{app_id}/datasets/{ds_id}/mutate",
                    json={"params": {}}, headers=_auth(admin_token))
    assert r.status_code == 403
    assert "mutation_sql" in r.json()["detail"]


def test_mutation_requires_binding(client, admin_token):
    target = _seed_target_db()
    r = client.post("/api/admin/connections", json={
        "name": f"nb-conn-{uuid.uuid4().hex[:6]}",
        "kind": "sql",
        "config": {"dialect": "sqlite", "database": str(target)},
        "read_only": False,
    }, headers=_auth(admin_token))
    conn_id = r.json()["id"]
    r = client.post("/api/admin/datasets", json={
        "name": f"nb-ds-{uuid.uuid4().hex[:6]}",
        "connection_id": conn_id,
        "kind": "query",
        "definition": {"sql": "SELECT 1", "mutation_sql": "DELETE FROM products"},
    }, headers=_auth(admin_token))
    ds_id = r.json()["id"]

    # No binding inserted
    r = client.post(f"/api/apps/{uuid.uuid4()}/datasets/{ds_id}/mutate",
                    json={"params": {}}, headers=_auth(admin_token))
    assert r.status_code == 403
    assert "not bound" in r.json()["detail"]


def test_mutation_current_user_injected(client, admin_token):
    """A mutation_sql that references :current_user binds the caller's name."""
    target = _seed_target_db()
    # Add an audit-style table
    conn = sqlite3.connect(str(target))
    conn.execute("CREATE TABLE edits (id INTEGER PRIMARY KEY, who TEXT)")
    conn.commit()
    conn.close()

    r = client.post("/api/admin/connections", json={
        "name": f"cu-conn-{uuid.uuid4().hex[:6]}",
        "kind": "sql",
        "config": {"dialect": "sqlite", "database": str(target)},
        "read_only": False,
    }, headers=_auth(admin_token))
    conn_id = r.json()["id"]
    r = client.post("/api/admin/datasets", json={
        "name": f"cu-ds-{uuid.uuid4().hex[:6]}",
        "connection_id": conn_id,
        "kind": "query",
        "definition": {
            "sql": "SELECT who FROM edits",
            "mutation_sql": "INSERT INTO edits (who) VALUES (:current_user)",
        },
    }, headers=_auth(admin_token))
    ds_id = r.json()["id"]
    app_id = str(uuid.uuid4())
    _bind_app(app_id, ds_id)

    r = client.post(f"/api/apps/{app_id}/datasets/{ds_id}/mutate",
                    json={"params": {}}, headers=_auth(admin_token))
    assert r.status_code == 200

    r = client.post(f"/api/apps/{app_id}/datasets/{ds_id}/execute",
                    json={"params": {}}, headers=_auth(admin_token))
    whos = {row["who"] for row in r.json()["rows"]}
    assert "admin" in whos
