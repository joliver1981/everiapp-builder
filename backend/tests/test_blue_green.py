"""Blue/green deploy: stand up green alongside blue, health-check, cut over —
or abort and leave blue serving.

Build + deployer are stubbed (no agent / no real build); the orchestration +
cutover/abort decision is what's under test.
"""
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
_DB = _TMP / "test_blue_green.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_blue_green")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "blue-green-test")

from src.config import settings  # noqa: E402
from src.database import async_session, init_db  # noqa: E402
from src.deployments import service as dsvc  # noqa: E402
from src.deployments.deployers.base import HealthResult  # noqa: E402
from src.deployments.service import deployments_service  # noqa: E402
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
def admin_id(client):
    client.post("/api/auth/login", json={"username": "admin", "password": "password"})
    conn = sqlite3.connect(settings.database_url[len("sqlite+aiosqlite:///"):])
    try:
        return conn.execute("SELECT id FROM users WHERE username='admin' LIMIT 1").fetchone()[0]
    finally:
        conn.close()


def _conn():
    return sqlite3.connect(settings.database_url[len("sqlite+aiosqlite:///"):])


def _seed(admin_id):
    target_id = str(uuid.uuid4())
    app_id = str(uuid.uuid4())
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO deployment_targets (id, name, kind, host, port, port_range_start, "
            "port_range_end, environment, extra_config, is_active, created_at, updated_at) "
            "VALUES (?, ?, 'agent', 'localhost', 8765, 9100, 9199, 'dev', '{}', 1, "
            "datetime('now'), datetime('now'))", (target_id, f"t-{target_id[:8]}"))
        conn.execute(
            "INSERT INTO apps (id, name, description, icon, status, current_version, "
            "ai_toggle_enabled, bug_widget_enabled, bug_fix_auto_approve_max_risk, "
            "ai_verify_level, ai_verify_max_iterations, created_by, created_at, updated_at) "
            "VALUES (?, ?, '', 'app-window', 'published', 2, 0, 0, 'none', 'tsc_build_boot', 8, ?, "
            "datetime('now'), datetime('now'))", (app_id, f"bg-{app_id[:8]}", admin_id))
        for v in (1, 2):
            conn.execute(
                "INSERT INTO app_versions (id, app_id, version, notes, published_by, manifest, created_at) "
                "VALUES (?, ?, ?, '', ?, ?, datetime('now'))",
                (str(uuid.uuid4()), app_id, v, admin_id, json.dumps({})))
        # blue = v1 running on 9100
        blue_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO deployments (id, app_id, version, target_id, allocated_port, status, "
            "public_url, deployed_by, started_at, last_health_at, last_health_status, consecutive_health_failures) "
            "VALUES (?, ?, 1, ?, 9100, 'running', 'http://localhost:9100', ?, datetime('now'), "
            "datetime('now'), 'ok', 0)", (blue_id, app_id, target_id, admin_id))
        conn.commit()
    finally:
        conn.close()
    return app_id, target_id, blue_id


class _FakeBuilder:
    async def build_app(self, app_id, version):
        return f"/tmp/fake-{app_id}-{version}.tgz"


class _FakeDeployer:
    def __init__(self, healthy):
        self.healthy = healthy
        self.stopped: list[str] = []

    async def deploy(self, deployment, artifact, port):
        return f"http://localhost:{port}"

    async def health(self, deployment):
        return HealthResult(ok=self.healthy, detail="stub")

    async def stop(self, deployment):
        self.stopped.append(deployment.id)


def _patch(monkeypatch, healthy):
    fake = _FakeDeployer(healthy)
    monkeypatch.setattr(dsvc, "builder", _FakeBuilder())
    monkeypatch.setattr(dsvc, "get_deployer", lambda t, c: fake)
    return fake


def _status(dep_id):
    conn = _conn()
    try:
        r = conn.execute("SELECT status FROM deployments WHERE id=?", (dep_id,)).fetchone()
        return r[0] if r else None
    finally:
        conn.close()


def _running_version(app_id):
    conn = _conn()
    try:
        r = conn.execute(
            "SELECT version FROM deployments WHERE app_id=? AND status='running' "
            "ORDER BY started_at DESC LIMIT 1", (app_id,)).fetchone()
        return r[0] if r else None
    finally:
        conn.close()


def test_blue_green_healthy_cuts_over(client, admin_id, monkeypatch):
    app_id, target_id, blue_id = _seed(admin_id)
    _patch(monkeypatch, healthy=True)

    async def _run():
        async with async_session() as db:
            return await deployments_service.blue_green_deploy(
                db, app_id, 2, target_id, admin_id, health_interval=0)
    result = asyncio.run(_run())

    assert result["switched"] is True
    assert result["green_version"] == 2
    assert result["retired_blue"] == blue_id
    assert _status(blue_id) == "stopped"      # blue retired
    assert _running_version(app_id) == 2      # green live


def test_blue_green_unhealthy_keeps_blue(client, admin_id, monkeypatch):
    app_id, target_id, blue_id = _seed(admin_id)
    _patch(monkeypatch, healthy=False)

    async def _run():
        async with async_session() as db:
            return await deployments_service.blue_green_deploy(
                db, app_id, 2, target_id, admin_id, health_attempts=2, health_interval=0)
    result = asyncio.run(_run())

    assert result["switched"] is False
    assert result["reason"] == "green_unhealthy"
    assert _status(blue_id) == "running"      # blue still serving
    assert _running_version(app_id) == 1      # never cut over to v2

    # the green deployment was retired (failed)
    conn = _conn()
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM deployments WHERE app_id=? AND version=2 AND status='failed'",
            (app_id,)).fetchone()[0]
    finally:
        conn.close()
    assert n == 1
