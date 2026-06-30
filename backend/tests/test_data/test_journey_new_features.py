"""JOURNEYS for the Wave 2/3 features that just landed:

  - License install + show as an admin operator
  - Build a "Team Todos" app that uses the per-app SQLite store via /db endpoints
  - Tag PII columns on a dataset → confirm the data never leaks via runtime

These are written as multi-step human-shaped flows, not isolated assertions —
the goal is to catch the "every part works alone but together breaks" bug class.
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
_DB = _TMP / "test_journey_new_features.db"
if _DB.exists():
    try:
        _DB.unlink()
    except OSError:
        pass

os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_new_features")
os.environ["DEBUG"] = "true"
os.environ.setdefault(
    "MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8="
)
os.environ.setdefault("JWT_SECRET_KEY", "test-journey-secret")

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
def admin_token(client: TestClient) -> str:
    r = client.post("/api/auth/login", json={"username": "admin", "password": "password"})
    return r.json()["access_token"]


def _auth(t: str) -> dict:
    return {"Authorization": f"Bearer {t}"}


# ---------------------------------------------------------------------------
# JOURNEY 1: Admin operator installs a license
# ---------------------------------------------------------------------------
@pytest.mark.journey
def test_admin_installs_a_license_via_the_admin_api(client: TestClient, admin_token: str):
    """Mike from IT opens the platform after install, sees the unlicensed
    banner, requests a license JWT from us, then POSTs it via the admin API.
    The license info round-trips."""
    from src.licensing import license as lic

    # 1. Admin checks the current license — it's unlicensed
    r = client.get("/api/admin/license", headers=_auth(admin_token))
    # During tests no real license is loaded; either unlicensed or whatever was
    # set last. The tier should be unlicensed unless explicitly installed in
    # this test session.
    assert r.status_code == 200
    info = r.json()
    # Whatever it is, the response shape must include all the fields the UI uses
    for k in ("sub", "tier", "status", "seats", "features", "is_active"):
        assert k in info

    # 2. Admin pastes a license JWT (we issue one for the test)
    token = lic.issue_license(
        sub="Mike's Org",
        seats=25,
        tier="pro",
        days_valid=90,
        features=["app_db", "datasets"],
    )
    r = client.post(
        "/api/admin/license",
        json={"token": token},
        headers=_auth(admin_token),
    )
    assert r.status_code == 200, r.text
    installed = r.json()
    assert installed["sub"] == "Mike's Org"
    assert installed["tier"] == "pro"
    assert installed["seats"] == 25
    assert installed["is_active"] is True
    assert installed["days_remaining"] is not None and installed["days_remaining"] >= 89

    # 3. Refetching via GET shows the now-active license
    r = client.get("/api/admin/license", headers=_auth(admin_token))
    assert r.status_code == 200
    assert r.json()["sub"] == "Mike's Org"


@pytest.mark.journey
def test_admin_rejects_a_garbage_license(client: TestClient, admin_token: str):
    """Pasting a malformed JWT must return 400 with a useful message."""
    r = client.post(
        "/api/admin/license",
        json={"token": "this-is-not-a-jwt-at-all"},
        headers=_auth(admin_token),
    )
    assert r.status_code == 400
    assert "License rejected" in r.json()["detail"]


# ---------------------------------------------------------------------------
# JOURNEY 2: Build a Team Todos app on the per-app SQLite store
# ---------------------------------------------------------------------------
@pytest.mark.journey
def test_team_todos_app_lifecycle(client: TestClient, admin_token: str):
    """Sarah (the team lead) builds a 'Team Todos' app that uses the per-app
    SQLite store. Her React code (via the new SDK hooks) will POST to
    /api/apps/{id}/db/exec and /query. We drive those endpoints directly.

    Flow:
      1. Create an app (we do this via direct DB insert to skip scaffolding)
      2. Admin POSTs a migration to create the todos table
      3. Add 3 todos via /db/exec
      4. Read all todos via /db/query
      5. Mark one as done
      6. Verify the row count + done state via /db/query
      7. Inspect the data via the admin browser endpoint
    """
    # 1. App row
    app_id = str(uuid.uuid4())
    from . import _helpers as _airdb
    _airdb.insert_app_and_binding(None, app_id, dataset_id=None)

    # 2. Apply migrations
    r = client.post(
        f"/api/apps/{app_id}/db/migrate",
        json={
            "migrations": [
                {
                    "version": 1,
                    "name": "init_todos",
                    "sql": (
                        "CREATE TABLE todos ("
                        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                        "  title TEXT NOT NULL,"
                        "  done BOOLEAN DEFAULT 0,"
                        "  created_by TEXT,"
                        "  created_at TEXT DEFAULT CURRENT_TIMESTAMP"
                        ")"
                    ),
                },
                {
                    "version": 2,
                    "name": "add_priority",
                    "sql": "ALTER TABLE todos ADD COLUMN priority INTEGER DEFAULT 0",
                },
            ]
        },
        headers=_auth(admin_token),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied_versions"] == [1, 2]
    assert body["current_version"] == 2

    # 3. Insert 3 todos — each one auto-stamped with the calling user (admin)
    for title in ("Ship Wave 1A", "Write journey tests", "Demo to Mike"):
        r = client.post(
            f"/api/apps/{app_id}/db/exec",
            json={
                "sql": "INSERT INTO todos (title, created_by) VALUES (:t, :current_user)",
                "params": {"t": title},
            },
            headers=_auth(admin_token),
        )
        assert r.status_code == 200, r.text
        assert r.json()["rows_affected"] == 1

    # 4. Read all
    r = client.post(
        f"/api/apps/{app_id}/db/query",
        json={"sql": "SELECT id, title, done, created_by FROM todos ORDER BY id", "params": {}},
        headers=_auth(admin_token),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["row_count"] == 3
    assert all(r["created_by"] == "admin" for r in data["rows"])
    assert {r["title"] for r in data["rows"]} == {
        "Ship Wave 1A", "Write journey tests", "Demo to Mike",
    }

    # 5. Mark "Demo to Mike" as done
    r = client.post(
        f"/api/apps/{app_id}/db/exec",
        json={
            "sql": "UPDATE todos SET done = 1 WHERE title = :t",
            "params": {"t": "Demo to Mike"},
        },
        headers=_auth(admin_token),
    )
    assert r.status_code == 200
    assert r.json()["rows_affected"] == 1

    # 6. Verify done count
    r = client.post(
        f"/api/apps/{app_id}/db/query",
        json={"sql": "SELECT COUNT(*) AS n FROM todos WHERE done = 1", "params": {}},
        headers=_auth(admin_token),
    )
    assert r.json()["rows"][0]["n"] == 1

    # 7. Admin browser shows todos table + 3 rows
    r = client.get(f"/api/apps/{app_id}/db/tables", headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    tables = r.json()["tables"]
    by_name = {t["name"]: t for t in tables}
    assert "todos" in by_name
    assert by_name["todos"]["row_count"] == 3
    # Has priority column (added by migration 2)
    col_names = [c["name"] for c in by_name["todos"]["columns"]]
    assert "priority" in col_names


@pytest.mark.journey
def test_destructive_migration_refused_via_api(client: TestClient, admin_token: str):
    """An admin tries to push a migration that drops a table without the
    AIHUB-DESTRUCTIVE-OK marker — the API refuses, no data is touched."""
    app_id = str(uuid.uuid4())
    from . import _helpers as _airdb
    _airdb.insert_app_and_binding(None, app_id, dataset_id=None)

    # Set up a table
    client.post(
        f"/api/apps/{app_id}/db/migrate",
        json={"migrations": [
            {"version": 1, "name": "init", "sql": "CREATE TABLE notes (id INTEGER PRIMARY KEY)"}
        ]},
        headers=_auth(admin_token),
    )

    # Try a destructive migration without the marker
    r = client.post(
        f"/api/apps/{app_id}/db/migrate",
        json={"migrations": [
            {"version": 2, "name": "wipe", "sql": "DROP TABLE notes"}
        ]},
        headers=_auth(admin_token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["applied_versions"] == []
    assert len(body["refused"]) == 1

    # The table is still there
    r = client.get(f"/api/apps/{app_id}/db/tables", headers=_auth(admin_token))
    by_name = {t["name"]: t for t in r.json()["tables"]}
    assert "notes" in by_name


# ---------------------------------------------------------------------------
# JOURNEY 3: PII tagging round-trips on dataset CRUD
# ---------------------------------------------------------------------------
@pytest.mark.journey
def test_dataset_pii_tags_round_trip(client: TestClient, admin_token: str):
    """Admin creates a dataset with PII tags on email/ssn → tags come back on
    GET → admin updates them via PUT → still round-trip."""
    # Need a connection first
    r = client.post(
        "/api/admin/connections",
        json={
            "name": f"pii-conn-{uuid.uuid4().hex[:6]}",
            "kind": "sql",
            "config": {"dialect": "sqlite", "database": ":memory:"},
        },
        headers=_auth(admin_token),
    )
    conn_id = r.json()["id"]

    # Create with PII tags
    r = client.post(
        "/api/admin/datasets",
        json={
            "name": f"customers-{uuid.uuid4().hex[:6]}",
            "connection_id": conn_id,
            "kind": "query",
            "definition": {"sql": "SELECT 'alice' AS name, 'a@e.com' AS email, '111-22-3333' AS ssn"},
            "pii_tags": {"email": "email", "ssn": "ssn"},
        },
        headers=_auth(admin_token),
    )
    assert r.status_code == 201, r.text
    ds = r.json()
    assert ds["pii_tags"] == {"email": "email", "ssn": "ssn"}

    # GET round-trips
    r = client.get(f"/api/admin/datasets/{ds['id']}", headers=_auth(admin_token))
    assert r.json()["pii_tags"] == {"email": "email", "ssn": "ssn"}

    # Update — add 'phone' tag
    r = client.put(
        f"/api/admin/datasets/{ds['id']}",
        json={"pii_tags": {"email": "email", "ssn": "ssn", "phone": "phone"}},
        headers=_auth(admin_token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["pii_tags"] == {"email": "email", "ssn": "ssn", "phone": "phone"}


@pytest.mark.journey
def test_runtime_redacts_pii_columns(client: TestClient, admin_token: str):
    """A dataset that returns tagged PII columns must surface [REDACTED]
    at runtime — not the actual value."""
    from . import _helpers as _airdb

    # Connection
    r = client.post(
        "/api/admin/connections",
        json={
            "name": f"pii-rt-conn-{uuid.uuid4().hex[:6]}",
            "kind": "sql",
            "config": {"dialect": "sqlite", "database": ":memory:"},
        },
        headers=_auth(admin_token),
    )
    conn_id = r.json()["id"]

    # Dataset with PII-tagged columns
    r = client.post(
        "/api/admin/datasets",
        json={
            "name": f"pii-rt-ds-{uuid.uuid4().hex[:6]}",
            "connection_id": conn_id,
            "kind": "query",
            "definition": {
                "sql": (
                    "SELECT 'alice' AS name, 'alice@example.com' AS email, "
                    "'333-12-9999' AS ssn"
                )
            },
            "pii_tags": {"email": "email", "ssn": "ssn"},
        },
        headers=_auth(admin_token),
    )
    ds_id = r.json()["id"]

    # Bind to an app
    app_id = str(uuid.uuid4())
    _airdb.insert_app_and_binding(None, app_id, dataset_id=ds_id)

    # Execute via runtime
    r = client.post(
        f"/api/apps/{app_id}/datasets/{ds_id}/execute",
        json={"params": {}},
        headers=_auth(admin_token),
    )
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert rows[0]["name"] == "alice"             # unchanged — not tagged
    assert rows[0]["email"] == "[REDACTED]"       # redacted
    assert rows[0]["ssn"] == "[REDACTED]"         # redacted
    # And the raw email never appeared anywhere in the response body
    assert "alice@example.com" not in r.text
    assert "333-12-9999" not in r.text
