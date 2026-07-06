"""SDK re-vendoring on preview start — via the real /runtime/start route.

Existing generated apps carry the SDK snapshotted at generation time, so SDK
fixes (session-expiry handling, the deployed-app AIToggle URL bug) never
reached them. Every draft preview start now converges src/sdk to the current
app-template copy. Locked-in behaviors:

  - a stale SDK file is overwritten with the template bytes;
  - files the app owns (outside src/sdk) and EXTRA sdk files not in the
    template are never touched (no deletes — an old app may import one);
  - byte-identical files are NOT rewritten (a running Vite watches these:
    a gratuitous write = HMR reload = the preview-reset class of v0.7.x);
  - version snapshots (source='vN') are immutable — never re-vendored.
"""
import asyncio
import os
import tempfile
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_sdk_revendor.db"
if _DB.exists():
    try:
        _DB.unlink()
    except OSError:
        pass
_APPS_DIR = _TMP / "apps_sdk_revendor"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_APPS_DIR)
os.environ["DEBUG"] = "true"
os.environ.setdefault(
    "MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8="
)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

from src.config import settings  # noqa: E402
from src.main import app  # noqa: E402
from src.runtime.manager import runtime_manager  # noqa: E402

settings.app_data_dir = str(_APPS_DIR)

_TEMPLATE_SDK = Path(__file__).resolve().parents[3] / "app-template" / "src" / "sdk"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin_token(client: TestClient) -> str:
    r = client.post("/api/auth/login", json={"username": "admin", "password": "password"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(t: str) -> dict:
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture()
def started_app(client: TestClient, admin_token: str, monkeypatch):
    """An app row + seeded draft sdk dir + a stubbed _do_start (no real vite)."""
    r = client.post("/api/apps", json={"name": f"revendor-{uuid.uuid4().hex[:8]}"},
                    headers=_auth(admin_token))
    assert r.status_code in (200, 201), r.text
    app_id = r.json()["id"]

    sdk_dir = Path(settings.app_data_dir) / app_id / "draft" / "frontend" / "src" / "sdk"
    sdk_dir.mkdir(parents=True, exist_ok=True)

    async def fake_do_start(app_proc, source):
        app_proc.status = "running"
        runtime_manager._set_phase(app_proc, "running", "fake ready")
    monkeypatch.setattr(runtime_manager, "_do_start", fake_do_start)

    yield app_id, sdk_dir

    client.post(f"/api/apps/{app_id}/runtime/stop", headers=_auth(admin_token))


def test_start_revendors_stale_sdk_and_leaves_the_rest_alone(client, admin_token, started_app):
    app_id, sdk_dir = started_app

    # A stale SDK file (drifted vendored copy), an extra sdk file the template
    # doesn't have, and an app-owned file outside src/sdk.
    stale = sdk_dir / "tracing.ts"
    stale.write_text("// OLD vendored copy without the session fixes\n", encoding="utf-8")
    extra = sdk_dir / "legacyHelper.ts"
    extra.write_text("// only this old app has me\n", encoding="utf-8")
    app_file = sdk_dir.parent / "App.tsx"
    app_file.write_text("// the app's own code\n", encoding="utf-8")
    # DELETE a template file: POST /api/apps scaffolds the full current
    # template, so without this the "new file appears" assertion below would
    # pass even if the sync never created missing files. An old app predating
    # session.ts is exactly this state.
    missing = sdk_dir / "session.ts"
    if missing.exists():
        missing.unlink()

    r = client.post(f"/api/apps/{app_id}/runtime/start", json={"source": "draft"},
                    headers=_auth(admin_token))
    assert r.status_code == 200, r.text

    # Stale file converged to the template bytes (incl. the new session import).
    assert stale.read_bytes() == (_TEMPLATE_SDK / "tracing.ts").read_bytes()
    # Missing template files are CREATED (an old app gains new SDK modules).
    assert missing.read_bytes() == (_TEMPLATE_SDK / "session.ts").read_bytes()
    # Extras and app-owned files are untouched.
    assert extra.read_text(encoding="utf-8") == "// only this old app has me\n"
    assert app_file.read_text(encoding="utf-8") == "// the app's own code\n"


def test_start_never_rewrites_identical_files(client, admin_token, started_app):
    app_id, sdk_dir = started_app

    # Seed a file already byte-identical to the template, aged into the past.
    current = sdk_dir / "session.ts"
    current.write_bytes((_TEMPLATE_SDK / "session.ts").read_bytes())
    old = time.time() - 86400
    os.utime(current, (old, old))

    r = client.post(f"/api/apps/{app_id}/runtime/start", json={"source": "draft"},
                    headers=_auth(admin_token))
    assert r.status_code == 200, r.text

    # mtime unchanged → the file was not rewritten → no HMR churn for a
    # running Vite. (Windows FAT granularity is 2s; a day dwarfs it.)
    assert abs(current.stat().st_mtime - old) < 5


def test_versioned_start_leaves_snapshots_immutable(client, admin_token, started_app):
    app_id, sdk_dir = started_app

    version_sdk = Path(settings.app_data_dir) / app_id / "versions" / "v1" / "src" / "sdk"
    version_sdk.mkdir(parents=True, exist_ok=True)
    frozen = version_sdk / "tracing.ts"
    frozen.write_text("// frozen v1 snapshot\n", encoding="utf-8")

    r = client.post(f"/api/apps/{app_id}/runtime/start", json={"source": "v1"},
                    headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    assert frozen.read_text(encoding="utf-8") == "// frozen v1 snapshot\n"


def test_sync_helper_is_a_noop_without_a_vendored_dir():
    """Apps mid-scaffold (no src/sdk yet) must not explode or gain files."""
    from src.apps.service import sync_vendored_sdk

    empty = _TMP / f"no-sdk-{uuid.uuid4().hex[:6]}" / "frontend"
    empty.mkdir(parents=True, exist_ok=True)
    assert sync_vendored_sdk(empty) == []
    assert not (empty / "src" / "sdk").exists()


def test_generation_cannot_write_into_vendored_sdk(client, admin_token, started_app):
    """Rule 7 ('never edit src/sdk') is now enforced in CODE, not just the
    prompt: preview start re-vendors src/sdk, so a model edit there would
    verify green and then be silently REVERTED on the next Start — apps
    importing the customization would break with no visible cause. Blocking
    the write makes the violation loud at generation time instead."""
    from src.ai.schemas import GeneratedFile
    from src.ai.service import ai_service

    app_id, sdk_dir = started_app
    sdk_target = sdk_dir / "useDataset.ts"
    sdk_target.write_text("// vendored — must survive\n", encoding="utf-8")

    asyncio.run(ai_service._save_generated_files(app_id, [
        GeneratedFile(path="src/sdk/useDataset.ts", content="// MODEL EDIT", action="modify"),
        GeneratedFile(path="src/sdk/evil-new.ts", content="// MODEL ADD", action="create"),
        GeneratedFile(path="src/OkFile.tsx", content="// legit app code\n", action="create"),
    ]))

    assert sdk_target.read_text(encoding="utf-8") == "// vendored — must survive\n"
    assert not (sdk_dir / "evil-new.ts").exists()
    assert (sdk_dir.parent / "OkFile.tsx").read_text(encoding="utf-8") == "// legit app code\n"
