"""App creation without the 140MB node_modules copy + lazy dependency provisioning.

POST /api/apps used to copytree the entire app-template INCLUDING node_modules
(~140MB / 11.5k files, ~40s on Windows) — app creation blocked on it, and it
once dominated this suite's runtime. Locked-in behaviors:

  - scaffold copies the template but NEVER node_modules;
  - try_copy_template_node_modules provisions offline from the template only
    when the app's declared deps match the template's, and refuses when they
    drifted or the template itself was never npm-installed (callers then fall
    back to a real npm install);
  - a stale partial dir from an interrupted copy is replaced, and an
    already-present node_modules is left untouched;
  - verifier stage 0 (ensure_node_modules) uses the template copy, so a fresh
    app's first verify needs no npm/network.
"""
import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.config import settings
from src.main import app
from src.apps import provisioning


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
    r = client.post("/api/apps", json={"name": f"prov-{uuid.uuid4().hex[:6]}"},
                    headers=_auth(token))
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


# ---- scaffold: template yes, node_modules no --------------------------------

def test_create_app_scaffolds_template_without_node_modules(client, admin_token):
    app_id = _create_app(client, admin_token)
    draft = Path(settings.app_data_dir) / app_id / "draft" / "frontend"
    assert (draft / "package.json").is_file(), "template was not scaffolded"
    assert (draft / "src" / "sdk").is_dir(), "vendored SDK missing from scaffold"
    assert not (draft / "node_modules").exists(), \
        "scaffold must not copy the template's node_modules"


# ---- the provisioning helper (fake template, no real npm) -------------------

def _mk_template(root: Path, deps: dict) -> Path:
    t = root / "template"
    (t / "node_modules" / "vite").mkdir(parents=True)
    (t / "node_modules" / "vite" / "marker.txt").write_text("from-template", encoding="utf-8")
    (t / "package.json").write_text(json.dumps({"name": "tpl", "dependencies": deps}), encoding="utf-8")
    return t


def _mk_app_dir(root: Path, deps: dict) -> Path:
    a = root / "appdir"
    a.mkdir(parents=True)
    (a / "package.json").write_text(json.dumps({"name": "an-app", "dependencies": deps}), encoding="utf-8")
    return a


def test_template_copy_provisions_matching_app(tmp_path):
    t = _mk_template(tmp_path, {"react": "^19.0.0"})
    a = _mk_app_dir(tmp_path, {"react": "^19.0.0"})
    assert asyncio.run(provisioning.try_copy_template_node_modules(a, t)) is True
    assert (a / "node_modules" / "vite" / "marker.txt").read_text(encoding="utf-8") == "from-template"


def test_template_copy_refuses_drifted_deps(tmp_path):
    t = _mk_template(tmp_path, {"react": "^19.0.0"})
    a = _mk_app_dir(tmp_path, {"react": "^19.0.0", "lodash": "^4.0.0"})
    assert asyncio.run(provisioning.try_copy_template_node_modules(a, t)) is False
    assert not (a / "node_modules").exists()


def test_template_copy_refuses_when_template_uninstalled(tmp_path):
    t = tmp_path / "template"
    t.mkdir()
    (t / "package.json").write_text(json.dumps({"dependencies": {}}), encoding="utf-8")
    a = _mk_app_dir(tmp_path, {})
    assert asyncio.run(provisioning.try_copy_template_node_modules(a, t)) is False


def test_template_copy_leaves_existing_node_modules_alone(tmp_path):
    t = _mk_template(tmp_path, {"react": "^19.0.0"})
    a = _mk_app_dir(tmp_path, {"react": "^19.0.0"})
    mine = a / "node_modules"
    mine.mkdir()
    (mine / "mine.txt").write_text("app-owned", encoding="utf-8")
    assert asyncio.run(provisioning.try_copy_template_node_modules(a, t)) is True
    assert (mine / "mine.txt").is_file()
    assert not (mine / "vite").exists(), "existing node_modules must not be overwritten"


def test_template_copy_replaces_stale_partial_dir(tmp_path):
    """An interrupted earlier copy leaves .node_modules.partial-<pid>; a retry
    must clean it up and still provision successfully."""
    t = _mk_template(tmp_path, {"react": "^19.0.0"})
    a = _mk_app_dir(tmp_path, {"react": "^19.0.0"})
    stale = a / f".node_modules.partial-{os.getpid()}"
    stale.mkdir()
    (stale / "junk.txt").write_text("crashed copy leftovers", encoding="utf-8")
    assert asyncio.run(provisioning.try_copy_template_node_modules(a, t)) is True
    assert (a / "node_modules" / "vite" / "marker.txt").is_file()
    assert not stale.exists()


# ---- verifier stage 0 goes through the template copy ------------------------

def test_first_verify_provisions_from_template_without_npm(client, admin_token, monkeypatch, tmp_path):
    """A fresh app's first verify must not need npm/network: ensure_node_modules
    (stage 0 of every verify) provisions from the template copy."""
    from src.ai import verifier

    app_id = _create_app(client, admin_token)
    draft = Path(settings.app_data_dir) / app_id / "draft" / "frontend"

    # A fake installed template whose package.json matches the app's draft
    # byte-for-byte (the draft's package.json IS the template's — scaffold copied it).
    t = tmp_path / "template"
    (t / "node_modules").mkdir(parents=True)
    (t / "node_modules" / "marker.txt").write_text("provisioned", encoding="utf-8")
    (t / "package.json").write_text((draft / "package.json").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(provisioning, "TEMPLATE_DIR", t)

    async def _no_npm(*args, **kwargs):
        raise AssertionError("npm install must not run when the template copy suffices")
    monkeypatch.setattr(verifier, "_run", _no_npm)

    assert asyncio.run(verifier.ensure_node_modules(app_id)) is None
    assert (draft / "node_modules" / "marker.txt").is_file()
