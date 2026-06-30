"""Dependency-upgrade scanner: advisory rules + HTTP endpoint."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_dependency_scan.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_dependency_scan")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "dependency-scan-test")

from src.config import settings  # noqa: E402
from src.database import init_db  # noqa: E402
from src.dependency_scan.scanner import scan_text  # noqa: E402
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


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


def test_scan_text_rules():
    pkg = json.dumps({
        "dependencies": {
            "lodash": "4.17.20",   # below 4.17.21 → high
            "axios": "^1.6.2",     # safe
            "react": "*",          # loose → low
            "request": "^2.88.0",  # deprecated → medium
        },
        "devDependencies": {"vite": "4.5.0"},  # below 4.5.2 → high
    })
    findings = scan_text(pkg)
    by_pkg = {f.package: f for f in findings}
    assert by_pkg["lodash"].severity == "high"
    assert by_pkg["request"].severity == "medium"
    assert by_pkg["react"].severity == "low"
    assert by_pkg["vite"].severity == "high"
    assert "axios" not in by_pkg  # safe version → no finding
    # worst-first ordering
    assert findings[0].severity == "high"


def test_scan_text_empty_and_invalid():
    assert scan_text("{}") == []
    assert scan_text("not json") == []
    # all-safe
    assert scan_text(json.dumps({"dependencies": {"axios": "^1.7.0"}})) == []


def _insert_app(app_id):
    db_path = settings.database_url[len("sqlite+aiosqlite:///"):]
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT id FROM users WHERE username='admin' LIMIT 1").fetchone()
        conn.execute(
            "INSERT INTO apps (id, name, description, icon, status, current_version, "
            "ai_toggle_enabled, bug_widget_enabled, bug_fix_auto_approve_max_risk, "
            "ai_verify_level, ai_verify_max_iterations, created_by, created_at, updated_at) "
            "VALUES (?, ?, '', 'app-window', 'draft', 0, 0, 0, 'none', 'tsc_build_boot', 8, ?, "
            "datetime('now'), datetime('now'))",
            (app_id, f"dep-{app_id[:8]}", row[0]),
        )
        conn.commit()
    finally:
        conn.close()


def test_http_dependency_scan(client, admin_token):
    app_id = str(uuid.uuid4())
    _insert_app(app_id)
    pkg = Path(settings.app_data_dir) / app_id / "draft" / "frontend" / "package.json"
    pkg.parent.mkdir(parents=True, exist_ok=True)
    pkg.write_text(json.dumps({"dependencies": {"lodash": "4.17.0", "moment": "2.29.0"}}), encoding="utf-8")

    r = client.get(f"/api/apps/{app_id}/dependency-scan", headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["package_json_found"] is True
    assert body["counts"]["high"] >= 1   # lodash
    pkgs = {f["package"] for f in body["findings"]}
    assert "lodash" in pkgs and "moment" in pkgs


def test_http_no_package_json(client, admin_token):
    app_id = str(uuid.uuid4())
    _insert_app(app_id)
    r = client.get(f"/api/apps/{app_id}/dependency-scan", headers=_auth(admin_token))
    assert r.status_code == 200
    assert r.json()["package_json_found"] is False
