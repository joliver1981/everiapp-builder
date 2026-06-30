"""Accessibility verify stage: level registration, findings mapper, routing,
and HTTP validation of the new ai_verify_level.

The in-browser audit itself needs a real Chromium (opt-in, like the runtime
probe) so it isn't exercised here; the logic around it is.
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
_DB = _TMP / "test_a11y_verify.db"
if _DB.exists():
    try:
        _DB.unlink()
    except OSError:
        # Locked by a stale/parallel process — use a unique file so collection
        # never aborts with WinError 32. The test still gets a clean DB.
        _DB = _TMP / f"test_a11y_verify_{uuid.uuid4().hex[:8]}.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_a11y_verify")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "a11y-verify-test")

from src.ai import verifier  # noqa: E402
from src.ai.verifier import VERIFY_LEVELS, VerifyResult, _a11y_findings_to_errors  # noqa: E402
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


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


def test_level_registered():
    assert "tsc_build_boot_runtime_a11y" in VERIFY_LEVELS


def test_findings_mapper():
    raw = [
        {"rule": "image-alt", "detail": "Image is missing an alt attribute", "selector": "img.logo"},
        {"rule": "label", "detail": "input form control has no associated label", "selector": "input#email"},
    ]
    errs = _a11y_findings_to_errors(raw)
    assert len(errs) == 2
    assert all(e.stage == "a11y" for e in errs)
    assert errs[0].code == "image-alt"
    assert "image-alt" in errs[0].message and "img.logo" in errs[0].message
    # Robust to empties / junk
    assert _a11y_findings_to_errors(None) == []
    assert _a11y_findings_to_errors([]) == []


def test_verify_app_routes_a11y(monkeypatch):
    captured = {}

    async def ok(*a, **k):
        return VerifyResult(stage_reached="ok", summary="ok")

    async def fake_runtime(app_id, run_a11y=False):
        captured["run_a11y"] = run_a11y
        return VerifyResult(stage_reached="runtime", summary="ok")

    monkeypatch.setattr(verifier, "run_tsc", ok)
    monkeypatch.setattr(verifier, "run_build", ok)
    monkeypatch.setattr(verifier, "run_boot_probe", ok)
    monkeypatch.setattr(verifier, "run_runtime_probe", fake_runtime)

    asyncio.run(verifier.verify_app("app1", "tsc_build_boot_runtime_a11y"))
    assert captured["run_a11y"] is True

    captured.clear()
    asyncio.run(verifier.verify_app("app1", "tsc_build_boot_runtime"))
    assert captured["run_a11y"] is False


def test_http_accepts_a11y_level(client, admin_token):
    # Insert an app owned by admin
    app_id = str(uuid.uuid4())
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
            (app_id, f"a11y-{app_id[:8]}", row[0]),
        )
        conn.commit()
    finally:
        conn.close()

    r = client.put(f"/api/apps/{app_id}", json={"ai_verify_level": "tsc_build_boot_runtime_a11y"},
                   headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    assert r.json()["ai_verify_level"] == "tsc_build_boot_runtime_a11y"

    # Invalid level rejected
    r = client.put(f"/api/apps/{app_id}", json={"ai_verify_level": "totally-bogus"},
                   headers=_auth(admin_token))
    assert r.status_code in (400, 422)
