"""Static security scanner + publish-gate tests.

The scanner runs over an app's draft source and the publish endpoint refuses to
snapshot code that trips a high/critical rule (unless an admin overrides).

We write draft files to `settings.app_data_dir` resolved AT CALL TIME and insert
the app row by reading `settings.database_url` at call time, so the test is
robust to cross-file engine/settings binding (pytest imports every test module
before running, so the singleton may point at another file's dir/db).
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
_DB = _TMP / "test_security_scan.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_security_scan")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "security-scan-test")

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
    r = client.post("/api/auth/login", json={"username": "admin", "password": "password"})
    return r.json()["access_token"]


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


def _write_draft(app_id: str, rel: str, content: str):
    p = Path(settings.app_data_dir) / app_id / "draft" / "frontend" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _insert_app(app_id: str):
    """Insert a minimal app row owned by admin (admin must have logged in)."""
    url = settings.database_url
    db_path = url[len("sqlite+aiosqlite:///"):]
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT id FROM users WHERE username='admin' LIMIT 1").fetchone()
        assert row, "admin user must exist — depend on admin_token fixture first"
        conn.execute(
            "INSERT OR IGNORE INTO apps (id, name, description, icon, status, current_version, "
            "ai_toggle_enabled, bug_widget_enabled, bug_fix_auto_approve_max_risk, "
            "ai_verify_level, ai_verify_max_iterations, created_by, created_at, updated_at) "
            "VALUES (?, ?, '', 'app-window', 'draft', 0, 0, 0, 'none', 'tsc_build_boot', 8, ?, "
            "datetime('now'), datetime('now'))",
            (app_id, f"sec-{app_id[:8]}", row[0]),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Scanner unit-level behavior (rules fire on real issues, not on safe code)
# ---------------------------------------------------------------------------
def test_scanner_flags_real_issues():
    from src.security_scan.scanner import scan_app
    app_id = str(uuid.uuid4())
    _write_draft(app_id, "src/App.tsx", (
        'const AWS = "AKIAIOSFODNN7EXAMPLE"\n'
        'const x = eval("1+1")\n'
        'function run(userId) {\n'
        '  return useAppQuery(`SELECT * FROM users WHERE id = ${userId}`)\n'
        '}\n'
        'export default function App() { return null }\n'
    ))
    report = scan_app(app_id)
    ids = {f.rule_id for f in report.findings}
    assert "aws-access-key" in ids
    assert "no-eval" in ids
    assert "sql-string-interpolation" in ids
    assert report.max_severity == "critical"
    assert report.counts["critical"] >= 1


def test_scanner_avoids_false_positives():
    """Patterns that look risky but aren't must NOT be flagged."""
    from src.security_scan.scanner import scan_app
    app_id = str(uuid.uuid4())
    _write_draft(app_id, "src/Safe.tsx", (
        'export function Safe() {\n'
        '  const apiBase = import.meta.env.VITE_API_KEY  // env read, not a literal\n'
        '  const msg = `Deleted ${count} rows`           // word-boundary guard\n'
        '  return <input type="password" autoComplete="off" />\n'
        '}\n'
    ))
    report = scan_app(app_id)
    ids = [f.rule_id for f in report.findings]
    assert "hardcoded-secret" not in ids
    assert "sql-string-interpolation" not in ids
    assert "no-eval" not in ids


# ---------------------------------------------------------------------------
# Publish gate end-to-end
# ---------------------------------------------------------------------------
def test_clean_app_publishes(client, admin_token):
    app_id = str(uuid.uuid4())
    _insert_app(app_id)
    _write_draft(app_id, "src/App.tsx", "export default function App(){return <div>hi</div>}")
    _write_draft(app_id, "package.json", '{"name":"clean"}')
    r = client.post(f"/api/apps/{app_id}/versions", json={"notes": "clean"}, headers=_auth(admin_token))
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 1


def test_publish_blocked_then_admin_override(client, admin_token):
    app_id = str(uuid.uuid4())
    _insert_app(app_id)
    _write_draft(app_id, "src/App.tsx",
                 'const k = "AKIAIOSFODNN7EXAMPLE"\nexport default function App(){return null}')

    # The scan endpoint reports it as blocking
    r = client.post(f"/api/apps/{app_id}/security-scan", headers=_auth(admin_token))
    assert r.status_code == 200
    assert r.json()["blocked"] is True
    assert r.json()["report"]["counts"]["critical"] >= 1

    # Publish is refused with a structured 422
    r = client.post(f"/api/apps/{app_id}/versions", json={"notes": "v1"}, headers=_auth(admin_token))
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"] == "security_scan_blocked"

    # Admin override succeeds and records an audit row
    r = client.post(f"/api/apps/{app_id}/versions",
                    json={"notes": "v1", "override_security": True}, headers=_auth(admin_token))
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 1

    db_path = settings.database_url[len("sqlite+aiosqlite:///"):]
    conn = sqlite3.connect(db_path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action='app.publish.security_override' "
            "AND resource_id=?", (app_id,)).fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_developer_cannot_override_security(client, admin_token):
    dev = client.post("/api/auth/login",
                      json={"username": "developer", "password": "password"}).json()["access_token"]
    app_id = str(uuid.uuid4())
    _insert_app(app_id)
    _write_draft(app_id, "src/App.tsx", 'const k = "AKIAIOSFODNN7EXAMPLE"')
    # Even with the override flag set, a developer is not allowed to bypass.
    r = client.post(f"/api/apps/{app_id}/versions",
                    json={"notes": "x", "override_security": True}, headers=_auth(dev))
    assert r.status_code == 422, r.text
