"""app-template resolution + husk-draft healing (the packaged-install ENOENT bug).

Installed (PyInstaller onedir) builds resolved app-template by walking
__file__ parents, which lands next to the exe — while the bundle actually
ships the template under _internal/ (sys._MEIPASS). POST /api/apps then
scaffolded a silent husk (src/App.tsx only, no package.json), and the app's
first AI generation failed at verify stage 0 with the opaque

    npm error enoent Could not read package.json:
    ...\\data\\apps\\<id>\\draft\\frontend\\package.json

Locked-in behaviors:
  - template_root() honors $AIHUB_APP_TEMPLATE_DIR, then sys._MEIPASS in
    frozen builds, then the repo layout;
  - a missing template fails app creation loudly (no husk left behind);
  - heal_missing_scaffold restores missing scaffold files, add-only;
  - verifier stage 0 and draft preview start both heal husks, so apps broken
    by an older build recover on their next verify/preview after upgrading.
"""
import asyncio
import json
import shutil
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src import app_template
from src.config import settings
from src.main import app

_REPO_ROOT = Path(__file__).resolve().parents[2]


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


def _create_app(client: TestClient, token: str) -> str:
    r = client.post("/api/apps", json={"name": f"tpl-{uuid.uuid4().hex[:6]}"},
                    headers=_auth(token))
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


# ---- template_root() resolution ---------------------------------------------

def test_template_root_dev_layout(monkeypatch):
    monkeypatch.delenv("AIHUB_APP_TEMPLATE_DIR", raising=False)
    root = app_template.template_root()
    assert root == _REPO_ROOT / "app-template"
    assert (root / "package.json").is_file()


def test_template_root_frozen_uses_meipass(monkeypatch, tmp_path):
    """THE regression lock: a PyInstaller build must look under sys._MEIPASS
    (where installer/aihub.spec bundles the template), not next to the exe."""
    monkeypatch.delenv("AIHUB_APP_TEMPLATE_DIR", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_internal"), raising=False)
    assert app_template.template_root() == tmp_path / "_internal" / "app-template"


def test_template_root_env_override_wins(monkeypatch, tmp_path):
    override = tmp_path / "custom-template"
    monkeypatch.setenv("AIHUB_APP_TEMPLATE_DIR", str(override))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_internal"), raising=False)
    assert app_template.template_root() == override


# ---- app creation fails loudly when the template is unresolvable ------------

def test_create_app_fails_loudly_when_template_missing(client, admin_token, monkeypatch, tmp_path):
    """No silent husk: the old fallback 'created' an app with no package.json
    that only failed minutes later, deep inside generation, as an npm ENOENT."""
    from src.apps import service as apps_service_module

    apps_root = Path(settings.app_data_dir)
    before = set(p.name for p in apps_root.iterdir()) if apps_root.is_dir() else set()

    monkeypatch.setattr(apps_service_module, "template_root",
                        lambda: tmp_path / "does-not-exist")
    r = client.post("/api/apps", json={"name": "husk-repro"}, headers=_auth(admin_token))
    assert r.status_code == 500, r.text
    assert "app-template" in r.json()["detail"]

    # Nothing committed, nothing scaffolded.
    after = set(p.name for p in apps_root.iterdir()) if apps_root.is_dir() else set()
    assert after == before, "a draft directory was left behind for the failed creation"
    listing = client.get("/api/apps", headers=_auth(admin_token))
    assert all(a["name"] != "husk-repro" for a in listing.json())


# ---- heal_missing_scaffold: add-only repair ---------------------------------

def _mk_fake_template(root: Path) -> Path:
    t = root / "template"
    (t / "src" / "sdk").mkdir(parents=True)
    (t / "package.json").write_text(json.dumps({"name": "tpl", "dependencies": {}}),
                                    encoding="utf-8")
    (t / "vite.config.ts").write_text("// template vite config\n", encoding="utf-8")
    (t / "src" / "main.tsx").write_text("// template entry\n", encoding="utf-8")
    (t / "src" / "sdk" / "index.ts").write_text("// template sdk\n", encoding="utf-8")
    (t / "node_modules").mkdir()
    (t / "node_modules" / "junk.txt").write_text("deps", encoding="utf-8")
    (t / "dist").mkdir()
    (t / "dist" / "junk.js").write_text("built", encoding="utf-8")
    return t


def test_heal_missing_scaffold_adds_only_whats_missing(tmp_path):
    t = _mk_fake_template(tmp_path)
    a = tmp_path / "appdir"
    (a / "src").mkdir(parents=True)
    (a / "src" / "App.tsx").write_text("// AI-written app code\n", encoding="utf-8")
    (a / "vite.config.ts").write_text("// app-customized config\n", encoding="utf-8")

    added = app_template.heal_missing_scaffold(a, t)

    assert sorted(added) == ["package.json", "src/main.tsx", "src/sdk/index.ts"]
    # Existing files are never overwritten…
    assert (a / "vite.config.ts").read_text(encoding="utf-8") == "// app-customized config\n"
    assert (a / "src" / "App.tsx").read_text(encoding="utf-8") == "// AI-written app code\n"
    # …and derived state is never copied.
    assert not (a / "node_modules").exists()
    assert not (a / "dist").exists()
    # Idempotent: a healthy tree heals to nothing.
    assert app_template.heal_missing_scaffold(a, t) == []


def test_heal_is_a_noop_when_template_or_app_missing(tmp_path):
    # App dir missing (template resolvable): nothing to do, nothing created.
    assert app_template.heal_missing_scaffold(tmp_path / "no-app") == []
    assert not (tmp_path / "no-app").exists()
    # Template missing: never explodes, never invents files.
    a = tmp_path / "some-app"
    a.mkdir()
    assert app_template.heal_missing_scaffold(a, tmp_path / "no-tpl") == []


# ---- verifier stage 0 heals a husk draft ------------------------------------

def test_verifier_heals_husk_draft_before_provisioning(client, admin_token, monkeypatch, tmp_path):
    """The upgrade path for apps broken by the old build: verify stage 0
    restores the scaffold, then provisions node_modules offline — no npm."""
    from src.ai import verifier
    from src.apps import provisioning

    app_id = _create_app(client, admin_token)
    draft = Path(settings.app_data_dir) / app_id / "draft" / "frontend"

    # Recreate the husk the old silent fallback produced: src/App.tsx only.
    shutil.rmtree(draft)
    (draft / "src").mkdir(parents=True)
    (draft / "src" / "App.tsx").write_text("// AI-written app code\n", encoding="utf-8")

    # Fake installed template with node_modules ready to copy.
    t = tmp_path / "template"
    (t / "node_modules").mkdir(parents=True)
    (t / "node_modules" / "marker.txt").write_text("provisioned", encoding="utf-8")
    (t / "package.json").write_text(json.dumps({"name": "tpl", "dependencies": {"react": "^19.0.0"}}),
                                    encoding="utf-8")
    (t / "vite.config.ts").write_text("// template vite config\n", encoding="utf-8")

    monkeypatch.setattr(app_template, "template_root", lambda: t)  # heal's default
    monkeypatch.setattr(provisioning, "template_root", lambda: t)  # offline copy's default

    async def _no_npm(*args, **kwargs):
        raise AssertionError("npm install must not run — heal + template copy must suffice")
    monkeypatch.setattr(verifier, "_run", _no_npm)

    assert asyncio.run(verifier.ensure_node_modules(app_id)) is None
    assert (draft / "package.json").is_file(), "scaffold was not healed"
    assert (draft / "vite.config.ts").is_file()
    assert (draft / "src" / "App.tsx").read_text(encoding="utf-8") == "// AI-written app code\n"
    assert (draft / "node_modules" / "marker.txt").is_file()


# ---- preview start heals a husk draft ---------------------------------------

def test_preview_start_heals_husk_draft(client, admin_token, monkeypatch):
    from src.runtime.manager import runtime_manager

    app_id = _create_app(client, admin_token)
    draft = Path(settings.app_data_dir) / app_id / "draft" / "frontend"
    shutil.rmtree(draft)
    (draft / "src").mkdir(parents=True)
    (draft / "src" / "App.tsx").write_text("// AI-written app code\n", encoding="utf-8")

    async def fake_do_start(app_proc, source):
        app_proc.status = "running"
        runtime_manager._set_phase(app_proc, "running", "fake ready")
    monkeypatch.setattr(runtime_manager, "_do_start", fake_do_start)

    try:
        r = client.post(f"/api/apps/{app_id}/runtime/start", json={"source": "draft"},
                        headers=_auth(admin_token))
        assert r.status_code == 200, r.text
        # Healed from the real repo template before vite would have started.
        assert (draft / "package.json").is_file(), "scaffold was not healed on preview start"
        assert (draft / "vite.config.ts").is_file()
        assert (draft / "index.html").is_file()
        assert (draft / "src" / "App.tsx").read_text(encoding="utf-8") == "// AI-written app code\n"
    finally:
        client.post(f"/api/apps/{app_id}/runtime/stop", headers=_auth(admin_token))
