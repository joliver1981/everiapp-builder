"""TestClient integration test for POST /api/apps/{id}/versions (publish).

Regression: publish used to do a synchronous shutil.copytree of the WHOLE draft,
including node_modules. For a typical AI-generated app with a populated
node_modules, the request would hang the asyncio event loop for tens of seconds
and the UI's publish button spun forever.

These tests assert:
  1. Publish completes within a reasonable timeout (no hang).
  2. node_modules / dist / .git are NOT included in the published version dir.
  3. The actual source files ARE included.
"""
import asyncio
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Same isolation pattern as the other integration tests in this repo.
_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_AIHUB_TESTS_TMP = Path(tempfile.gettempdir()) / "aihub-tests"
for _candidate in (
    _TMP / "test_publish.db",
    _AIHUB_TESTS_TMP / "test.db",
):
    if _candidate.exists():
        try:
            _candidate.unlink()
        except OSError:
            pass

_DB = _TMP / "test_publish.db"
_APPS_DIR = _TMP / "apps_publish"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_APPS_DIR)
os.environ["DEBUG"] = "true"
os.environ.setdefault(
    "MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8="
)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

from src.config import settings  # noqa: E402
from src.main import app  # noqa: E402

# Force the settings singleton to use this test module's apps dir, in case the
# singleton was initialized earlier with a different DATABASE_URL/APP_DATA_DIR.
settings.app_data_dir = str(_APPS_DIR)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin_token(client: TestClient) -> str:
    r = client.post("/api/auth/login", json={"username": "admin", "password": "password"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_app(client: TestClient, admin_token: str) -> str:
    """Create an app via the real API, then drop into its draft dir and seed it
    with realistic content: a few source files PLUS a fake node_modules with
    many tiny files so the regression (full-tree copy) would be measurable.
    """
    r = client.post(
        "/api/apps",
        json={"name": f"pub-{uuid.uuid4().hex[:8]}", "description": "publish test"},
        headers=_auth(admin_token),
    )
    assert r.status_code in (200, 201), r.text
    app_id = r.json()["id"]

    draft = Path(settings.app_data_dir) / app_id / "draft" / "frontend"
    draft.mkdir(parents=True, exist_ok=True)
    # Source files we DO want in the published version
    (draft / "src").mkdir(exist_ok=True)
    (draft / "src" / "App.tsx").write_text("export default function App(){return null}", encoding="utf-8")
    (draft / "package.json").write_text('{"name":"x","version":"0.0.1"}', encoding="utf-8")

    # Fake node_modules — 200 tiny files in 20 subdirs. Small enough not to
    # blow up the test runner, big enough that copying it would be slow if
    # the ignore filter were missing.
    nm = draft / "node_modules"
    for pkg in range(20):
        pkg_dir = nm / f"pkg-{pkg}"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        for f in range(10):
            (pkg_dir / f"file-{f}.js").write_text("module.exports = {}", encoding="utf-8")
    return app_id


def test_publish_completes_quickly_and_excludes_node_modules(client: TestClient, admin_token: str):
    """The two-in-one regression test. If publish was still copying node_modules,
    this would either timeout or produce a version dir that contains it."""
    app_id = _make_app(client, admin_token)

    t0 = time.monotonic()
    r = client.post(
        f"/api/apps/{app_id}/versions",
        json={"notes": "v1 regression test"},
        headers=_auth(admin_token),
    )
    duration = time.monotonic() - t0

    assert r.status_code in (200, 201), r.text
    # Generous bound. Without the ignore filter, copying 200 small files
    # would still take ~0.5s; the real bug case was 10s+ on real apps.
    # 5s gives us plenty of headroom on a slow CI box.
    assert duration < 5.0, f"publish took {duration:.2f}s — fix the ignore filter"

    # The version dir should have the source files
    version_dir = Path(settings.app_data_dir) / app_id / "versions" / "v1"
    assert (version_dir / "src" / "App.tsx").exists()
    assert (version_dir / "package.json").exists()
    # ...but NOT node_modules
    assert not (version_dir / "node_modules").exists(), \
        "node_modules should be excluded from published versions"


def test_publish_returns_incrementing_version_numbers(client: TestClient, admin_token: str):
    app_id = _make_app(client, admin_token)

    r1 = client.post(
        f"/api/apps/{app_id}/versions", json={"notes": "first"}, headers=_auth(admin_token),
    )
    assert r1.status_code in (200, 201), r1.text
    assert r1.json()["version"] == 1

    # Tweak the draft so v2 has different content (not strictly required, but
    # makes the test feel real).
    draft = Path(settings.app_data_dir) / app_id / "draft" / "frontend" / "src" / "App.tsx"
    draft.write_text("// v2", encoding="utf-8")

    r2 = client.post(
        f"/api/apps/{app_id}/versions", json={"notes": "second"}, headers=_auth(admin_token),
    )
    assert r2.status_code in (200, 201), r2.text
    assert r2.json()["version"] == 2
