"""TestClient integration test for the rollback-draft + lkg endpoints.

Per the CLAUDE.md rule: every HTTP route gets exercised through the real
request pipeline (auth, routing, response serialization), not just the
service layer.
"""
import asyncio
import os
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Same DB-isolation pattern as the deployment-targets integration tests.
_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_AIHUB_TESTS_TMP = Path(tempfile.gettempdir()) / "aihub-tests"
for _candidate in (
    _TMP / "test_rollback.db",
    _AIHUB_TESTS_TMP / "test.db",
):
    if _candidate.exists():
        try:
            _candidate.unlink()
        except OSError:
            pass

_DB = _TMP / "test_rollback.db"
_APPS_DIR = _TMP / "apps_rollback"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_APPS_DIR)
os.environ["DEBUG"] = "true"
os.environ.setdefault(
    "MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8="
)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

from src.ai import snapshots  # noqa: E402
from src.config import settings  # noqa: E402
from src.main import app  # noqa: E402

# Force settings.app_data_dir to match what this test file declared in the env
# var above. The Settings() singleton may have been instantiated earlier by
# another test module (e.g. test_snapshots.py imports src.config at module
# level), which freezes settings.app_data_dir to the conftest default — leaving
# our os.environ override ignored by the code under test.
settings.app_data_dir = str(_APPS_DIR)


@pytest.fixture(scope="module")
def client():
    # TestClient(app) as a context-manager runs FastAPI lifespan, which calls
    # init_db once. A separate _init_db fixture would race with that and trigger
    # SQLite "database is locked" errors on subsequent test modules.
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin_token(client: TestClient) -> str:
    r = client.post("/api/auth/login", json={"username": "admin", "password": "password"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_app_with_draft(client: TestClient, admin_token: str, app_files: dict[str, str]) -> str:
    """Build an app row + its draft dir directly.

    Earlier versions of this helper POSTed to /api/apps, but that endpoint
    scaffolds the full app-template (copying node_modules takes ~30s on dev
    machines) and the slow scaffold racing with our manual file writes caused
    flaky 'wrong file content' assertions in the full pytest run. We don't
    need the template here — we just need a draft dir to snapshot.

    For the same reason we INSERT the App row directly via SQL rather than
    using apps_service.create_app, which also triggers scaffolding.
    """
    from sqlalchemy import text
    from src.database import async_session
    import asyncio

    app_id = str(uuid.uuid4())
    # Insert an App row so the rollback endpoint's permission checks find it.
    async def _insert():
        async with async_session() as s:
            # apps.created_by has FK to users.id. The test logged in as admin
            # before this helper runs, so the admin user row exists; use it.
            creator_row = (await s.execute(text(
                "SELECT id FROM users WHERE username = 'admin' LIMIT 1"
            ))).fetchone()
            creator_id = creator_row[0] if creator_row else None
            if creator_id is None:
                creator_row = (await s.execute(text("SELECT id FROM users LIMIT 1"))).fetchone()
                creator_id = creator_row[0] if creator_row else None
            if creator_id is None:
                raise RuntimeError("no users in DB — log in via /api/auth/login first")
            await s.execute(text(
                "INSERT INTO apps (id, name, description, icon, status, current_version, "
                "ai_toggle_enabled, bug_widget_enabled, bug_fix_auto_approve_max_risk, "
                "ai_verify_level, ai_verify_max_iterations, created_by, created_at, updated_at) "
                "VALUES (:id, :n, '', 'app-window', 'draft', 0, 0, 0, 'none', "
                "'tsc_build_boot', 8, :creator, datetime('now'), datetime('now'))"
            ), {"id": app_id, "n": f"test-{app_id[:8]}", "creator": creator_id})
            await s.commit()
    asyncio.get_event_loop().run_until_complete(_insert()) if False else asyncio.run(_insert())

    draft = Path(settings.app_data_dir) / app_id / "draft" / "frontend"
    draft.mkdir(parents=True, exist_ok=True)
    for rel, content in app_files.items():
        p = draft / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return app_id


def test_lkg_endpoint_reports_no_snapshot_when_none_exists(client: TestClient, admin_token: str):
    app_id = _create_app_with_draft(client, admin_token, {"src/App.tsx": "v1"})
    r = client.get(f"/api/apps/{app_id}/lkg", headers=_auth(admin_token))
    assert r.status_code == 200
    body = r.json()
    assert body["has_snapshot"] is False
    assert body["info"] is None


def test_lkg_endpoint_reports_snapshot_when_present(client: TestClient, admin_token: str):
    app_id = _create_app_with_draft(client, admin_token, {"src/App.tsx": "v1"})
    snapshots.snapshot(app_id, note="before bad turn")

    r = client.get(f"/api/apps/{app_id}/lkg", headers=_auth(admin_token))
    assert r.status_code == 200
    body = r.json()
    assert body["has_snapshot"] is True
    assert body["info"]["note"] == "before bad turn"


def test_rollback_restores_draft_files(client: TestClient, admin_token: str):
    app_id = _create_app_with_draft(client, admin_token, {
        "src/App.tsx": "GOOD",
        "src/util.ts": "export const x = 1",
    })
    snapshots.snapshot(app_id)

    # Mutate draft as if AI applied broken changes
    draft = Path(settings.app_data_dir) / app_id / "draft" / "frontend"
    (draft / "src/App.tsx").write_text("BROKEN", encoding="utf-8")
    (draft / "src/extra.ts").write_text("added by ai", encoding="utf-8")

    r = client.post(f"/api/apps/{app_id}/rollback-draft", headers=_auth(admin_token))
    assert r.status_code == 200
    assert r.json() == {"restored": True}

    assert (draft / "src/App.tsx").read_text(encoding="utf-8") == "GOOD"
    assert not (draft / "src/extra.ts").exists()  # AI-added file removed by revert


def test_rollback_404_when_no_snapshot(client: TestClient, admin_token: str):
    app_id = _create_app_with_draft(client, admin_token, {"src/App.tsx": "v1"})
    r = client.post(f"/api/apps/{app_id}/rollback-draft", headers=_auth(admin_token))
    assert r.status_code == 404


def test_rollback_requires_auth(client: TestClient):
    r = client.post("/api/apps/some-id/rollback-draft")
    assert r.status_code == 401
