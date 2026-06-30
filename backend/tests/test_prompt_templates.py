"""Prompt-library: built-in seeding + admin CRUD + developer read-only access."""
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
_DB = _TMP / "test_prompt_templates.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_prompt_templates")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "prompt-templates-test")

from src.database import init_db  # noqa: E402
from src.main import app  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _init():
    asyncio.run(init_db())
    yield


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:  # lifespan runs seed_builtins
        yield c


@pytest.fixture(scope="module")
def admin_token(client):
    return client.post("/api/auth/login", json={"username": "admin", "password": "password"}).json()["access_token"]


@pytest.fixture(scope="module")
def dev_token(client):
    return client.post("/api/auth/login", json={"username": "developer", "password": "password"}).json()["access_token"]


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


def test_builtins_seeded_and_listed(client, dev_token):
    r = client.get("/api/prompt-templates", headers=_auth(dev_token))
    assert r.status_code == 200
    items = r.json()
    builtins = [t for t in items if t["is_builtin"]]
    assert len(builtins) >= 5
    # Built-ins come first (ordering), and have a non-empty body
    assert items[0]["is_builtin"] is True
    assert all(t["body"] for t in builtins)
    assert any(t["id"] == "builtin-kanban" for t in builtins)


def test_admin_crud(client, admin_token, dev_token):
    title = f"Custom {uuid.uuid4().hex[:6]}"
    # Create
    r = client.post("/api/admin/prompt-templates", json={
        "title": title, "description": "my template", "category": "Custom",
        "body": "Build something bespoke",
    }, headers=_auth(admin_token))
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    assert r.json()["is_builtin"] is False

    # It shows up for developers
    r = client.get("/api/prompt-templates", headers=_auth(dev_token))
    assert any(t["id"] == tid for t in r.json())

    # Update
    r = client.put(f"/api/admin/prompt-templates/{tid}", json={"body": "Updated body"},
                   headers=_auth(admin_token))
    assert r.status_code == 200
    assert r.json()["body"] == "Updated body"

    # Delete
    r = client.delete(f"/api/admin/prompt-templates/{tid}", headers=_auth(admin_token))
    assert r.status_code == 204
    r = client.get("/api/prompt-templates", headers=_auth(dev_token))
    assert not any(t["id"] == tid for t in r.json())


def test_developer_cannot_manage(client, dev_token):
    r = client.post("/api/admin/prompt-templates", json={"title": "x", "body": "y"},
                    headers=_auth(dev_token))
    assert r.status_code in (401, 403)


def test_update_missing_returns_404(client, admin_token):
    r = client.put("/api/admin/prompt-templates/does-not-exist", json={"body": "z"},
                   headers=_auth(admin_token))
    assert r.status_code == 404
