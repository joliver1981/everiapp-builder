"""Scheduled backups + restore: create/list/prune, stage-restore, apply-on-restart.

The apply step targets TEMP paths (not the live DB) so the test never clobbers
its own database, and a teardown clears any staged-restore marker so it can't
leak into other test modules that share the engine.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_backups.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_backups")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "backups-test")

from src.backups import service as bsvc  # noqa: E402
from src.database import init_db  # noqa: E402
from src.main import app  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _init():
    asyncio.run(init_db())
    # Point app_data_dir at a TINY dir for the duration so create_backup doesn't
    # copytree the huge shared scaffold tree (the first-imported test file's
    # app_data_dir, bloated by other modules' apps) — keeps these tests fast in
    # the full suite. backup_dir() is derived from the DB path, not this, so it
    # is unaffected.
    from src.config import settings
    saved_app_dir = settings.app_data_dir
    small = _TMP / "backups_small_appdata"
    small.mkdir(parents=True, exist_ok=True)
    (small / "marker.txt").write_text("x", encoding="utf-8")
    settings.app_data_dir = str(small)
    yield
    settings.app_data_dir = saved_app_dir
    # Safety net: never leave a staged restore behind (shared engine/DB).
    try:
        m = bsvc.backup_dir() / ".pending-restore"
        if m.exists():
            m.unlink()
    except OSError:
        pass


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin_token(client):
    return client.post("/api/auth/login", json={"username": "admin", "password": "password"}).json()["access_token"]


@pytest.fixture(scope="module")
def dev_token(client):
    return client.post("/api/auth/login", json={"username": "developer", "password": "password"}).json()["access_token"]


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


def test_create_and_list_via_http(client, admin_token):
    r = client.post("/api/admin/backups", headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    name = r.json()["name"]
    assert name.startswith("aihub-backup-") and name.endswith(".tar.gz")
    assert r.json()["size_bytes"] > 0

    r = client.get("/api/admin/backups", headers=_auth(admin_token))
    assert any(b["name"] == name for b in r.json()["backups"])
    assert r.json()["pending_restore"] is None


def test_prune_keeps_newest_n():
    for _ in range(3):
        bsvc.create_backup()
    bsvc.prune_backups(2)
    assert len(bsvc.list_backups()) == 2


def test_stage_and_apply_restore(client, admin_token):
    info = bsvc.create_backup()
    name = info["name"]

    # Stage via HTTP
    r = client.post(f"/api/admin/backups/{name}/restore", headers=_auth(admin_token))
    assert r.status_code == 200
    assert r.json()["restart_required"] is True
    assert bsvc.pending_restore() == name

    # Apply to TEMP targets (simulating a restart) — never touches the live DB.
    tmp_db = _TMP / "restore_target.db"
    tmp_apps = _TMP / "restore_apps"
    if tmp_db.exists():
        tmp_db.unlink()
    applied = bsvc.apply_pending_restore(db_target=tmp_db, apps_target=tmp_apps)
    assert applied is True
    assert tmp_db.exists()

    # The restored DB is a valid platform DB.
    conn = sqlite3.connect(str(tmp_db))
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert "users" in tables

    # Marker cleared after apply.
    assert bsvc.pending_restore() is None


def test_restore_invalid_and_missing(client, admin_token):
    # Path-traversal / bad name is rejected. The encoded slash (%2F) decodes to
    # a multi-segment path that can't bind to the single-segment {name} param, so
    # it never reaches stage_restore — the security property we care about. The
    # exact status depends on whether the built SPA is present: with no dist the
    # unmatched POST is a 404; with the SPA catch-all (GET-only) mounted it's a
    # 405 (Allow: GET). Either way the traversal does not execute.
    r = client.post("/api/admin/backups/..%2Fetc/restore", headers=_auth(admin_token))
    assert r.status_code in (400, 404, 405)
    # A well-formed but non-existent name DOES reach the handler → 404.
    r = client.post("/api/admin/backups/aihub-backup-nope.tar.gz/restore", headers=_auth(admin_token))
    assert r.status_code == 404


def test_backups_admin_only(client, dev_token):
    assert client.get("/api/admin/backups", headers=_auth(dev_token)).status_code in (401, 403)
