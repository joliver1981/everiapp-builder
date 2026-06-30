"""Teams CRUD + membership + group-based app-access enforcement."""
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
_DB = _TMP / "test_teams_rbac.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_teams_rbac")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "teams-rbac-test")

from src.config import settings  # noqa: E402
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
    return client.post("/api/auth/login", json={"username": "admin", "password": "password"}).json()["access_token"]


@pytest.fixture(scope="module")
def user_ctx(client):
    r = client.post("/api/auth/login", json={"username": "user", "password": "password"}).json()
    return r["access_token"], r["user"]["id"]


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


def _insert_published_app(name: str) -> str:
    app_id = str(uuid.uuid4())
    conn = sqlite3.connect(settings.database_url[len("sqlite+aiosqlite:///"):])
    try:
        admin = conn.execute("SELECT id FROM users WHERE username='admin' LIMIT 1").fetchone()[0]
        conn.execute(
            "INSERT INTO apps (id, name, description, icon, status, current_version, "
            "ai_toggle_enabled, bug_widget_enabled, bug_fix_auto_approve_max_risk, "
            "ai_verify_level, ai_verify_max_iterations, created_by, created_at, updated_at) "
            "VALUES (?, ?, '', 'app-window', 'published', 1, 0, 0, 'none', 'tsc_build_boot', 8, ?, "
            "datetime('now'), datetime('now'))", (app_id, name, admin))
        conn.commit()
    finally:
        conn.close()
    return app_id


def _add_group_perm(app_id: str, group_name: str):
    conn = sqlite3.connect(settings.database_url[len("sqlite+aiosqlite:///"):])
    try:
        conn.execute(
            "INSERT INTO app_permissions (id, app_id, user_id, group_name, permission, created_at) "
            "VALUES (?, ?, NULL, ?, 'access', datetime('now'))",
            (str(uuid.uuid4()), app_id, group_name))
        conn.commit()
    finally:
        conn.close()


def test_team_crud_and_membership(client, admin_token, user_ctx):
    _, user_id = user_ctx
    r = client.post("/api/admin/teams", json={"name": "Engineering", "description": "eng"}, headers=_auth(admin_token))
    assert r.status_code == 201, r.text
    team_id = r.json()["id"]

    # duplicate name rejected
    assert client.post("/api/admin/teams", json={"name": "Engineering"}, headers=_auth(admin_token)).status_code == 400

    # list shows member_count 0
    teams = client.get("/api/admin/teams", headers=_auth(admin_token)).json()
    assert any(t["id"] == team_id and t["member_count"] == 0 for t in teams)

    # add member
    r = client.post(f"/api/admin/teams/{team_id}/members", json={"user_id": user_id}, headers=_auth(admin_token))
    assert r.status_code == 201
    members = client.get(f"/api/admin/teams/{team_id}/members", headers=_auth(admin_token)).json()
    assert any(m["user_id"] == user_id for m in members)
    teams = client.get("/api/admin/teams", headers=_auth(admin_token)).json()
    assert any(t["id"] == team_id and t["member_count"] == 1 for t in teams)

    # remove member
    assert client.delete(f"/api/admin/teams/{team_id}/members/{user_id}", headers=_auth(admin_token)).status_code == 204


def test_access_enforcement_open_vs_restricted(client, admin_token, user_ctx):
    user_token, user_id = user_ctx
    open_app = _insert_published_app(f"open-{uuid.uuid4().hex[:6]}")
    restricted = _insert_published_app(f"restricted-{uuid.uuid4().hex[:6]}")
    _add_group_perm(restricted, "SecretTeam")

    # As the end user: sees the open app, NOT the restricted one.
    apps = client.get("/api/apps", headers=_auth(user_token)).json()
    ids = {a["id"] for a in apps}
    assert open_app in ids
    assert restricted not in ids

    # Create the team + add the user → the restricted app becomes visible.
    team_id = client.post("/api/admin/teams", json={"name": "SecretTeam"}, headers=_auth(admin_token)).json()["id"]
    client.post(f"/api/admin/teams/{team_id}/members", json={"user_id": user_id}, headers=_auth(admin_token))

    apps = client.get("/api/apps", headers=_auth(user_token)).json()
    ids = {a["id"] for a in apps}
    assert restricted in ids   # now accessible via team membership


def test_direct_user_permission(client, admin_token, user_ctx):
    user_token, user_id = user_ctx
    app_id = _insert_published_app(f"direct-{uuid.uuid4().hex[:6]}")
    # restrict to a group the user is NOT in
    _add_group_perm(app_id, "NobodyTeam")
    assert app_id not in {a["id"] for a in client.get("/api/apps", headers=_auth(user_token)).json()}

    # grant the user directly
    conn = sqlite3.connect(settings.database_url[len("sqlite+aiosqlite:///"):])
    try:
        conn.execute(
            "INSERT INTO app_permissions (id, app_id, user_id, group_name, permission, created_at) "
            "VALUES (?, ?, ?, NULL, 'access', datetime('now'))",
            (str(uuid.uuid4()), app_id, user_id))
        conn.commit()
    finally:
        conn.close()
    assert app_id in {a["id"] for a in client.get("/api/apps", headers=_auth(user_token)).json()}


def test_teams_admin_only(client, user_ctx):
    user_token, _ = user_ctx
    assert client.get("/api/admin/teams", headers=_auth(user_token)).status_code in (401, 403)
