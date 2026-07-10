"""Per-app SQLite store via the REAL HTTP routes (TestClient, real auth).

Covers the query row cap end to end: the default is generous (no silent
truncation of an app's own data), an explicit `limit` is honored, and the
`truncated` flag is surfaced in the response envelope the SDK reads.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_app_db_http.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_app_db_http")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "app-db-http-test")

from src.database import init_db  # noqa: E402
from src.main import app as fastapi_app  # noqa: E402


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


def _seed_app(client, admin, n_rows: int) -> str:
    app_id = client.post("/api/apps", json={"name": f"db-{uuid.uuid4().hex[:6]}"}, headers=admin).json()["id"]
    r = client.post(f"/api/apps/{app_id}/db/migrate",
                    json={"migrations": [{"version": 1, "name": "init",
                                          "sql": "CREATE TABLE items (id INTEGER PRIMARY KEY, n INTEGER)"}]},
                    headers=admin)
    assert r.status_code == 200, r.text
    # Bulk-seed directly (the HTTP write path is rate-limited to ~120/burst; the
    # endpoint UNDER TEST is /db/query, which we hit over HTTP below).
    from src.app_db.service import _open
    conn = _open(app_id)
    try:
        conn.executemany("INSERT INTO items (n) VALUES (?)", [(i,) for i in range(n_rows)])
        conn.commit()
    finally:
        conn.close()
    return app_id


def test_query_default_returns_all_rows(client, admin):
    """More rows than the OLD 1000 cap come back whole and untruncated by default."""
    app_id = _seed_app(client, admin, 1200)
    r = client.post(f"/api/apps/{app_id}/db/query",
                    json={"sql": "SELECT id, n FROM items ORDER BY id"}, headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["row_count"] == 1200
    assert body["truncated"] is False


def test_query_limit_is_honored_and_truncated_surfaced(client, admin):
    """An explicit limit caps the result and the response reports truncated=True
    so the SDK (and the app) can tell the user more rows exist."""
    app_id = _seed_app(client, admin, 50)
    r = client.post(f"/api/apps/{app_id}/db/query",
                    json={"sql": "SELECT id FROM items ORDER BY id", "limit": 10}, headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["row_count"] == 10
    assert body["truncated"] is True


def test_independent_schema_declarations_all_apply(client, admin):
    """The Model Compare regression: an app declares its tables from SEVERAL
    useAppSchema hooks — each arrives as its own version-1 migration (distinct
    content-derived name). The old high-water-version gate applied only the
    FIRST and silently skipped the rest ('applied=[] refused=0' in the audit
    log), so every other table failed at runtime with 'no such table'. Each
    declaration must apply; re-sending one must still skip."""
    app_id = client.post("/api/apps", json={"name": f"db-{uuid.uuid4().hex[:6]}"},
                         headers=admin).json()["id"]

    def migrate(name, sql):
        r = client.post(f"/api/apps/{app_id}/db/migrate",
                        json={"migrations": [{"version": 1, "name": name, "sql": sql}]},
                        headers=admin)
        assert r.status_code == 200, r.text
        return r.json()

    # Two components each declare their own table — both at version 1.
    first = migrate("app_schema_aaaa", "CREATE TABLE IF NOT EXISTS prompts (id INTEGER PRIMARY KEY, name TEXT)")
    second = migrate("app_schema_bbbb", "CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, winner TEXT)")
    assert first["applied_versions"] == [1]
    assert second["applied_versions"] == [1], "second version-1 declaration was skipped"

    # Both tables usable through the real exec/query routes.
    for table in ("prompts", "runs"):
        col = "name" if table == "prompts" else "winner"
        r = client.post(f"/api/apps/{app_id}/db/exec",
                        json={"sql": f"INSERT INTO {table} ({col}) VALUES (:v)", "params": {"v": "x"}},
                        headers=admin)
        assert r.status_code == 200, f"{table}: {r.text}"
        r = client.post(f"/api/apps/{app_id}/db/query",
                        json={"sql": f"SELECT * FROM {table}"}, headers=admin)
        assert r.status_code == 200 and r.json()["row_count"] == 1

    # Re-mount (same declaration again) stays idempotent — nothing re-applies.
    again = migrate("app_schema_bbbb", "CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, winner TEXT)")
    assert again["applied_versions"] == []
