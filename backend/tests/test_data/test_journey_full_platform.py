"""JOURNEYS for the full-platform features built this round.

Human-shaped, multi-step flows of escalating difficulty:

  EASY:   Admin tours the Platform page — sets a custom prompt + budgets.
  MEDIUM: A team builds a CRUD app on the per-app store, then an admin tags PII
          on a dataset and confirms it's redacted at runtime.
  HARD:   Full lifecycle — admin sets a budget, configures an LDAP provider,
          a directory user logs in and gets the right role, builds an app with
          its own DB AND a writable dataset, and lineage + cost tracking all
          reflect the activity.

These are integration-level (TestClient), not live AIRDB, so they run in the
default gate.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_journey_full_platform.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_full_platform")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "full-platform-test")

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


def _bind(app_id, ds_id):
    from . import _helpers as _airdb
    _airdb.insert_app_and_binding(None, app_id, ds_id)


# ===========================================================================
# EASY — Admin tours the Platform page
# ===========================================================================
@pytest.mark.journey
def test_admin_configures_platform_settings(client, admin_token):
    # Read current license (unlicensed or whatever)
    r = client.get("/api/admin/license", headers=_auth(admin_token))
    assert r.status_code == 200

    # Install a license
    from src.licensing import license as lic
    token = lic.issue_license(sub="Journey Org", seats=20, tier="pro",
                              days_valid=180, features=["app_db", "datasets"])
    r = client.post("/api/admin/license", json={"token": token}, headers=_auth(admin_token))
    assert r.status_code == 200
    assert r.json()["sub"] == "Journey Org"

    # Set a custom system prompt + budgets
    r = client.put("/api/admin/settings", json={
        "custom_system_prompt": "Use our navy + gold palette.",
        "monthly_budget_usd": 500.0,
        "per_user_budget_usd": 50.0,
    }, headers=_auth(admin_token))
    assert r.status_code == 200
    assert r.json()["custom_system_prompt"].startswith("Use our navy")

    # Health + cost endpoints respond
    assert client.get("/api/admin/connections/health/all", headers=_auth(admin_token)).status_code == 200
    assert client.get("/api/admin/llm-usage/summary?days=30", headers=_auth(admin_token)).status_code == 200


# ===========================================================================
# MEDIUM — Team builds a CRUD app on the per-app store
# ===========================================================================
@pytest.mark.journey
def test_team_kanban_on_app_store(client, admin_token):
    """A team lead builds a Kanban app backed by the per-app SQLite store."""
    from . import _helpers as _airdb
    app_id = str(uuid.uuid4())
    _airdb.insert_app_and_binding(None, app_id, dataset_id=None)

    # Migrate: cards table
    r = client.post(f"/api/apps/{app_id}/db/migrate", json={"migrations": [
        {"version": 1, "name": "init", "sql": (
            "CREATE TABLE cards (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "title TEXT NOT NULL, column TEXT DEFAULT 'todo', created_by TEXT)"
        )},
    ]}, headers=_auth(admin_token))
    assert r.json()["applied_versions"] == [1]

    # Add 3 cards
    for title in ("Design", "Build", "Ship"):
        r = client.post(f"/api/apps/{app_id}/db/exec", json={
            "sql": "INSERT INTO cards (title, created_by) VALUES (:t, :current_user)",
            "params": {"t": title},
        }, headers=_auth(admin_token))
        assert r.json()["rows_affected"] == 1

    # Move "Design" to done
    client.post(f"/api/apps/{app_id}/db/exec", json={
        "sql": "UPDATE cards SET column = 'done' WHERE title = :t",
        "params": {"t": "Design"},
    }, headers=_auth(admin_token))

    # Read board grouped
    r = client.post(f"/api/apps/{app_id}/db/query", json={
        "sql": "SELECT column, COUNT(*) AS n FROM cards GROUP BY column",
        "params": {},
    }, headers=_auth(admin_token))
    counts = {row["column"]: row["n"] for row in r.json()["rows"]}
    assert counts.get("done") == 1
    assert counts.get("todo") == 2

    # Admin browses the data
    r = client.get(f"/api/apps/{app_id}/db/tables", headers=_auth(admin_token))
    by_name = {t["name"]: t for t in r.json()["tables"]}
    assert by_name["cards"]["row_count"] == 3


@pytest.mark.journey
def test_pii_redaction_end_to_end(client, admin_token):
    """Admin tags PII columns; runtime never leaks them."""
    target = _TMP / f"pii-{uuid.uuid4().hex[:8]}.db"
    conn = sqlite3.connect(str(target))
    conn.execute("CREATE TABLE people (id INTEGER, name TEXT, email TEXT, ssn TEXT)")
    conn.execute("INSERT INTO people VALUES (1, 'Dana', 'dana@corp.com', '555-00-1234')")
    conn.commit()
    conn.close()

    r = client.post("/api/admin/connections", json={
        "name": f"pii-j-conn-{uuid.uuid4().hex[:6]}", "kind": "sql",
        "config": {"dialect": "sqlite", "database": str(target)},
    }, headers=_auth(admin_token))
    conn_id = r.json()["id"]

    r = client.post("/api/admin/datasets", json={
        "name": f"people-{uuid.uuid4().hex[:6]}", "connection_id": conn_id,
        "kind": "query", "definition": {"sql": "SELECT id, name, email, ssn FROM people"},
        "pii_tags": {"email": "email", "ssn": "ssn"},
    }, headers=_auth(admin_token))
    ds_id = r.json()["id"]
    app_id = str(uuid.uuid4())
    _bind(app_id, ds_id)

    r = client.post(f"/api/apps/{app_id}/datasets/{ds_id}/execute", json={"params": {}},
                    headers=_auth(admin_token))
    row = r.json()["rows"][0]
    assert row["name"] == "Dana"
    assert row["email"] == "[REDACTED]"
    assert row["ssn"] == "[REDACTED]"
    assert "dana@corp.com" not in r.text and "555-00-1234" not in r.text


# ===========================================================================
# HARD — full lifecycle with LDAP + writable dataset + lineage + cache
# ===========================================================================
@pytest.mark.journey
def test_full_lifecycle_ldap_writable_dataset_lineage(client, admin_token):
    # 1. Configure an LDAP provider. First clear any provider left in the
    # shared test engine by another file so the chain has exactly ours
    # (test modules share one process-wide DB engine bound at first import).
    for p in client.get("/api/admin/auth-providers", headers=_auth(admin_token)).json():
        client.delete(f"/api/admin/auth-providers/{p['id']}", headers=_auth(admin_token))
    client.post("/api/admin/auth-providers", json={
        "provider_type": "ldap", "provider_name": "Journey AD",
        "config": {"server": "dc.journey.local", "bind_template": "{username}@journey.local"},
        "group_role_mapping": {"Engineers": "developer"},
        "default_role": "user", "auto_provision": True, "is_enabled": True, "is_default": True,
    }, headers=_auth(admin_token))

    # 2. A directory user logs in (mock the LDAP bind)
    entry = MagicMock()
    entry.displayName.value = "Eng User"
    entry.mail.value = "eng@journey.local"
    entry.sAMAccountName.value = "enguser"
    entry.memberOf.values = ["CN=Engineers,DC=journey,DC=local"]
    fake_conn = MagicMock()
    fake_conn.entries = [entry]
    with patch("src.auth.providers.ldap_provider.Connection", return_value=fake_conn):
        r = client.post("/api/auth/login", json={"username": "enguser", "password": "pw"})
    assert r.status_code == 200
    assert r.json()["user"]["role"] == "developer"

    # 3. Admin builds a WRITABLE connection + dataset with mutation_sql + cache
    target = _TMP / f"orders-{uuid.uuid4().hex[:8]}.db"
    c = sqlite3.connect(str(target))
    c.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT, item TEXT, qty INTEGER)")
    c.commit(); c.close()

    r = client.post("/api/admin/connections", json={
        "name": f"orders-conn-{uuid.uuid4().hex[:6]}", "kind": "sql",
        "config": {"dialect": "sqlite", "database": str(target)},
        "read_only": False,
    }, headers=_auth(admin_token))
    conn_id = r.json()["id"]

    r = client.post("/api/admin/datasets", json={
        "name": f"orders-ds-{uuid.uuid4().hex[:6]}", "connection_id": conn_id,
        "kind": "query",
        "definition": {
            "sql": "SELECT id, item, qty FROM orders ORDER BY id",
            "mutation_sql": "INSERT INTO orders (item, qty) VALUES (:item, :qty)",
        },
        "cache_ttl_seconds": 60,
    }, headers=_auth(admin_token))
    ds_id = r.json()["id"]
    assert r.json()["cache_ttl_seconds"] == 60

    app_id = str(uuid.uuid4())
    _bind(app_id, ds_id)

    # 4. App writes an order, then reads (read is cached)
    r = client.post(f"/api/apps/{app_id}/datasets/{ds_id}/mutate",
                    json={"params": {"item": "Widget", "qty": 5}}, headers=_auth(admin_token))
    assert r.json()["rows_affected"] == 1

    r = client.post(f"/api/apps/{app_id}/datasets/{ds_id}/execute",
                    json={"params": {}}, headers=_auth(admin_token))
    assert r.json()["row_count"] == 1
    assert r.json()["rows"][0]["item"] == "Widget"

    # 5. Lineage shows the connection + the bound app
    r = client.get(f"/api/admin/datasets/{ds_id}/lineage", headers=_auth(admin_token))
    assert r.status_code == 200
    lineage = r.json()
    assert lineage["connection"]["id"] == conn_id
    assert any(a["id"] == app_id for a in lineage["bound_apps"])

    # 6. A second write invalidates the cache → fresh read sees both rows
    client.post(f"/api/apps/{app_id}/datasets/{ds_id}/mutate",
                json={"params": {"item": "Gadget", "qty": 3}}, headers=_auth(admin_token))
    r = client.post(f"/api/apps/{app_id}/datasets/{ds_id}/execute",
                    json={"params": {}}, headers=_auth(admin_token))
    items = {row["item"] for row in r.json()["rows"]}
    assert items == {"Widget", "Gadget"}, "cache should have been invalidated by the write"
