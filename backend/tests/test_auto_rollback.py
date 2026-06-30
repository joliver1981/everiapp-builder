"""Deployment auto-rollback: redeploy the last healthy version on repeated
health failures.

We avoid real build/agent infra by stubbing the deployer (`get_deployer`) and
the `deploy()` call, and by seeding deployment rows directly. The logic under
test is the decision: counter increment, threshold gating, candidate selection,
and the audit trail.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_auto_rollback.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_auto_rollback")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "auto-rollback-test")

from src.config import settings  # noqa: E402
from src.database import async_session, init_db  # noqa: E402
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
def admin_token(client):
    return client.post("/api/auth/login", json={"username": "admin", "password": "password"}).json()["access_token"]


@pytest.fixture(scope="module")
def admin_id(client, admin_token):
    db_path = settings.database_url[len("sqlite+aiosqlite:///"):]
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT id FROM users WHERE username='admin' LIMIT 1").fetchone()[0]
    finally:
        conn.close()


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


def _conn():
    return sqlite3.connect(settings.database_url[len("sqlite+aiosqlite:///"):])


def _seed_target() -> str:
    tid = str(uuid.uuid4())
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO deployment_targets (id, name, kind, host, port, port_range_start, "
            "port_range_end, environment, extra_config, is_active, created_at, updated_at) "
            "VALUES (?, ?, 'agent', 'localhost', 8765, 9100, 9199, 'dev', '{}', 1, "
            "datetime('now'), datetime('now'))",
            (tid, f"tgt-{tid[:8]}"),
        )
        conn.commit()
    finally:
        conn.close()
    return tid


def _seed_app(admin_id: str) -> str:
    aid = str(uuid.uuid4())
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO apps (id, name, description, icon, status, current_version, "
            "ai_toggle_enabled, bug_widget_enabled, bug_fix_auto_approve_max_risk, "
            "ai_verify_level, ai_verify_max_iterations, created_by, created_at, updated_at) "
            "VALUES (?, ?, '', 'app-window', 'published', 2, 0, 0, 'none', 'tsc_build_boot', 8, ?, "
            "datetime('now'), datetime('now'))",
            (aid, f"ar-{aid[:8]}", admin_id),
        )
        conn.commit()
    finally:
        conn.close()
    return aid


def _seed_deployment(*, app_id, target_id, version, status, health, failures,
                     admin_id, offset_seconds, port) -> str:
    did = str(uuid.uuid4())
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO deployments (id, app_id, version, target_id, allocated_port, status, "
            "public_url, deployed_by, started_at, last_health_at, last_health_status, "
            "consecutive_health_failures) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now','+{offset_seconds} seconds'), "
            "datetime('now'), ?, ?)",
            (did, app_id, version, target_id, port, status,
             f"http://localhost:{port}", admin_id, health, failures),
        )
        conn.commit()
    finally:
        conn.close()
    return did


async def _call_rollback(deployment_id: str):
    async with async_session() as db:
        return await deployments_service.maybe_auto_rollback(db, deployment_id)


def test_rollback_redeploys_last_healthy_version(client, admin_token, admin_id, monkeypatch):
    client.put("/api/admin/settings",
               json={"auto_rollback_enabled": True, "auto_rollback_fail_threshold": 3},
               headers=_auth(admin_token))
    target_id = _seed_target()
    app_id = _seed_app(admin_id)
    # v1 healthy (stopped), v2 currently failing (running, 3 failures)
    _seed_deployment(app_id=app_id, target_id=target_id, version=1, status="stopped",
                     health="ok", failures=0, admin_id=admin_id, offset_seconds=1, port=9100)
    v2 = _seed_deployment(app_id=app_id, target_id=target_id, version=2, status="running",
                          health="error", failures=3, admin_id=admin_id, offset_seconds=2, port=9101)

    calls = []

    async def fake_deploy(db, a_id, version, t_id, user_id):
        calls.append((a_id, version, t_id, user_id))
        return SimpleNamespace(id=str(uuid.uuid4()), version=version)

    monkeypatch.setattr(deployments_service, "deploy", fake_deploy)
    result = asyncio.run(_call_rollback(v2))

    assert result is not None
    assert len(calls) == 1
    assert calls[0][0] == app_id
    assert calls[0][1] == 1          # rolled back to the healthy v1
    assert calls[0][2] == target_id

    # Audit row recorded
    conn = _conn()
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action='deployment.auto_rollback'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n >= 1


def test_no_rollback_below_threshold(client, admin_token, admin_id, monkeypatch):
    client.put("/api/admin/settings",
               json={"auto_rollback_enabled": True, "auto_rollback_fail_threshold": 3},
               headers=_auth(admin_token))
    target_id = _seed_target()
    app_id = _seed_app(admin_id)
    _seed_deployment(app_id=app_id, target_id=target_id, version=1, status="stopped",
                     health="ok", failures=0, admin_id=admin_id, offset_seconds=1, port=9100)
    v2 = _seed_deployment(app_id=app_id, target_id=target_id, version=2, status="running",
                          health="error", failures=2, admin_id=admin_id, offset_seconds=2, port=9101)
    calls = []
    monkeypatch.setattr(deployments_service, "deploy",
                        lambda *a, **k: calls.append(a) or SimpleNamespace(id="x"))
    assert asyncio.run(_call_rollback(v2)) is None
    assert calls == []


def test_no_rollback_when_disabled(client, admin_token, admin_id, monkeypatch):
    client.put("/api/admin/settings", json={"auto_rollback_enabled": False},
               headers=_auth(admin_token))
    target_id = _seed_target()
    app_id = _seed_app(admin_id)
    _seed_deployment(app_id=app_id, target_id=target_id, version=1, status="stopped",
                     health="ok", failures=0, admin_id=admin_id, offset_seconds=1, port=9100)
    v2 = _seed_deployment(app_id=app_id, target_id=target_id, version=2, status="running",
                          health="error", failures=9, admin_id=admin_id, offset_seconds=2, port=9101)
    calls = []
    monkeypatch.setattr(deployments_service, "deploy",
                        lambda *a, **k: calls.append(a) or SimpleNamespace(id="x"))
    assert asyncio.run(_call_rollback(v2)) is None
    assert calls == []
    # reset for other tests sharing the DB
    client.put("/api/admin/settings", json={"auto_rollback_enabled": False},
               headers=_auth(admin_token))


def test_no_rollback_without_healthy_candidate(client, admin_token, admin_id, monkeypatch):
    client.put("/api/admin/settings",
               json={"auto_rollback_enabled": True, "auto_rollback_fail_threshold": 3},
               headers=_auth(admin_token))
    target_id = _seed_target()
    app_id = _seed_app(admin_id)
    # The only prior version is itself unhealthy → no candidate
    _seed_deployment(app_id=app_id, target_id=target_id, version=1, status="failed",
                     health="error", failures=5, admin_id=admin_id, offset_seconds=1, port=9100)
    v2 = _seed_deployment(app_id=app_id, target_id=target_id, version=2, status="running",
                          health="error", failures=4, admin_id=admin_id, offset_seconds=2, port=9101)
    calls = []
    monkeypatch.setattr(deployments_service, "deploy",
                        lambda *a, **k: calls.append(a) or SimpleNamespace(id="x"))
    assert asyncio.run(_call_rollback(v2)) is None
    assert calls == []
    client.put("/api/admin/settings", json={"auto_rollback_enabled": False},
               headers=_auth(admin_token))


def test_health_check_counter_increments_and_resets(client, admin_token, admin_id, monkeypatch):
    target_id = _seed_target()
    app_id = _seed_app(admin_id)
    dep = _seed_deployment(app_id=app_id, target_id=target_id, version=1, status="running",
                           health="ok", failures=0, admin_id=admin_id, offset_seconds=1, port=9100)

    state = {"ok": False}

    class _FakeDeployer:
        async def health(self, deployment):
            return HealthResult(ok=state["ok"], detail="stub")

    monkeypatch.setattr("src.deployments.service.get_deployer", lambda t, c: _FakeDeployer())

    async def _check():
        async with async_session() as db:
            return await deployments_service.health_check(db, dep)

    asyncio.run(_check())
    asyncio.run(_check())

    def _failures():
        conn = _conn()
        try:
            return conn.execute(
                "SELECT consecutive_health_failures FROM deployments WHERE id=?", (dep,)
            ).fetchone()[0]
        finally:
            conn.close()

    assert _failures() == 2
    state["ok"] = True
    asyncio.run(_check())
    assert _failures() == 0


def test_give_up_stops_chronically_unhealthy_deployment(client, admin_id):
    """A deployment that keeps failing health (and can't roll back) is marked
    'stopped' so the health loop stops probing a dead deployment."""
    target_id = _seed_target()
    app_id = _seed_app(admin_id)
    # Running deployment already past the give-up threshold, no healthy fallback.
    dep = _seed_deployment(app_id=app_id, target_id=target_id, version=1, status="running",
                           health="error", failures=deployments_service.GIVE_UP_FAILURES,
                           admin_id=admin_id, offset_seconds=1, port=9100)

    def _status(dep_id):
        conn = _conn()
        try:
            return conn.execute("SELECT status FROM deployments WHERE id=?", (dep_id,)).fetchone()[0]
        finally:
            conn.close()

    async def _run():
        async with async_session() as db:
            return await deployments_service.maybe_give_up(db, dep)
    assert asyncio.run(_run()) is True
    assert _status(dep) == "stopped"

    # Below the threshold → left running.
    dep2 = _seed_deployment(app_id=app_id, target_id=target_id, version=1, status="running",
                            health="error", failures=2, admin_id=admin_id, offset_seconds=2, port=9101)

    async def _run2():
        async with async_session() as db:
            return await deployments_service.maybe_give_up(db, dep2)
    assert asyncio.run(_run2()) is False
    assert _status(dep2) == "running"
