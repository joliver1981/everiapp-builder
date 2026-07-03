"""App package export/import via the real HTTP routes.

Round-trips a published app through GET /api/apps/{id}/export and
POST /api/apps/import, and locks in the package-safety rejections
(tampered checksum, path traversal, wrong kind/schema).
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import tempfile
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_packaging.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_packaging")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "packaging-test")

from src.config import settings  # noqa: E402
from src.database import init_db  # noqa: E402
from src.main import app  # noqa: E402
from src.packaging.service import MANIFEST_NAME, PACKAGE_KIND, PACKAGE_SCHEMA  # noqa: E402

WIZARD = {"steps": [{"fields": [{"key": "api_key", "label": "API key",
                                 "type": "secret", "required": True}]}]}


@pytest.fixture(scope="module", autouse=True)
def _init():
    asyncio.run(init_db())
    yield


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def token(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "password"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture(scope="module")
def published_app(client, token):
    """An app with real draft files, a setup wizard, and one published version."""
    r = client.post("/api/apps", json={
        "name": "Pack Test App", "description": "exports cleanly", "icon": "rocket",
    }, headers=_auth(token))
    assert r.status_code == 201, r.text
    app_id = r.json()["id"]

    # Drop a recognizable file into the draft alongside the template scaffold.
    draft = Path(settings.app_data_dir) / app_id / "draft" / "frontend"
    draft.mkdir(parents=True, exist_ok=True)
    (draft / "src").mkdir(exist_ok=True)
    (draft / "src" / "marker.ts").write_text("export const MARKER = 'pack-test-42'\n")

    r = client.put(f"/api/apps/{app_id}/wizard", json=WIZARD, headers=_auth(token))
    assert r.status_code == 200, r.text
    saved_wizard = r.json()  # router stores model_dump(): {"steps": [...], "title": None}

    r = client.post(f"/api/apps/{app_id}/versions", json={"notes": "first"},
                    headers=_auth(token))
    assert r.status_code == 201, r.text
    return {"id": app_id, "wizard": saved_wizard}


@pytest.fixture(scope="module")
def exported_zip(client, token, published_app):
    r = client.get(f"/api/apps/{published_app['id']}/export", headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    assert ".zip" in r.headers["content-disposition"]
    return r.content


def test_export_requires_auth(client, published_app):
    assert client.get(f"/api/apps/{published_app['id']}/export").status_code in (401, 403)


def test_export_unknown_app_404(client, token):
    assert client.get("/api/apps/no-such-app/export", headers=_auth(token)).status_code == 404


def test_export_unpublished_app_400(client, token):
    r = client.post("/api/apps", json={"name": "Never Published"}, headers=_auth(token))
    app_id = r.json()["id"]
    r = client.get(f"/api/apps/{app_id}/export", headers=_auth(token))
    assert r.status_code == 400
    assert "no published versions" in r.json()["detail"]


def test_export_package_shape(exported_zip, published_app):
    zf = zipfile.ZipFile(io.BytesIO(exported_zip))
    names = zf.namelist()
    assert MANIFEST_NAME in names
    manifest = json.loads(zf.read(MANIFEST_NAME))
    assert manifest["kind"] == PACKAGE_KIND
    assert manifest["schema"] == PACKAGE_SCHEMA
    assert manifest["name"] == "Pack Test App"
    assert manifest["slug"] == "pack-test-app"
    assert manifest["version"] == 1
    assert manifest["source_app_id"] == published_app["id"]

    # Every zipped file is declared with a correct checksum; marker file rode along.
    assert "src/marker.ts" in manifest["files"]
    for rel, meta in manifest["files"].items():
        data = zf.read(f"app/{rel}")
        assert hashlib.sha256(data).hexdigest() == meta["checksum"]
        assert len(data) == meta["size"]
    assert all(n == MANIFEST_NAME or n.startswith("app/") for n in names)
    assert not any("node_modules" in n for n in names)


def test_import_round_trip(client, token, exported_zip, published_app):
    r = client.post("/api/apps/import",
                    files={"file": ("pack.zip", exported_zip, "application/zip")},
                    headers=_auth(token))
    assert r.status_code == 201, r.text
    new_id = r.json()["app_id"]
    assert new_id != published_app["id"]

    # New app is a draft with the package's identity.
    r = client.get(f"/api/apps/{new_id}", headers=_auth(token))
    body = r.json()
    assert body["name"] == "Pack Test App"
    assert body["status"] == "draft"
    assert body["current_version"] == 0
    assert body["setup_wizard"] == published_app["wizard"]

    # Files landed byte-for-byte (draft holds exactly the package contents).
    new_draft = Path(settings.app_data_dir) / new_id / "draft" / "frontend"
    assert (new_draft / "src" / "marker.ts").read_text() == "export const MARKER = 'pack-test-42'\n"
    manifest = json.loads(zipfile.ZipFile(io.BytesIO(exported_zip)).read(MANIFEST_NAME))
    on_disk = {p.relative_to(new_draft).as_posix() for p in new_draft.rglob("*") if p.is_file()}
    assert on_disk == set(manifest["files"])


def test_import_requires_auth(client, exported_zip):
    r = client.post("/api/apps/import",
                    files={"file": ("pack.zip", exported_zip, "application/zip")})
    assert r.status_code in (401, 403)


def _repack(zip_bytes: bytes, mutate) -> bytes:
    """Rewrite a package zip, letting `mutate(name, data) -> (name, data) | None` edit entries."""
    src = zipfile.ZipFile(io.BytesIO(zip_bytes))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for name in src.namelist():
            item = mutate(name, src.read(name))
            if item is not None:
                dst.writestr(item[0], item[1])
    return out.getvalue()


def test_import_rejects_tampered_file(client, token, exported_zip):
    tampered = _repack(exported_zip, lambda n, d: (
        (n, d + b"// evil") if n == "app/src/marker.ts" else (n, d)))
    r = client.post("/api/apps/import",
                    files={"file": ("t.zip", tampered, "application/zip")},
                    headers=_auth(token))
    assert r.status_code == 400
    assert "Checksum mismatch" in r.json()["detail"]


def test_import_rejects_path_traversal(client, token, exported_zip):
    evil = _repack(exported_zip, lambda n, d: (n, d))
    # Append a traversal entry declared in no manifest.
    buf = io.BytesIO(evil)
    with zipfile.ZipFile(buf, "a", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("app/../../evil.txt", b"boom")
    r = client.post("/api/apps/import",
                    files={"file": ("e.zip", buf.getvalue(), "application/zip")},
                    headers=_auth(token))
    assert r.status_code == 400
    assert "Unsafe file path" in r.json()["detail"]


def test_import_rejects_undeclared_file(client, token, exported_zip):
    buf = io.BytesIO(exported_zip)
    with zipfile.ZipFile(buf, "a", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("app/src/stowaway.ts", b"export {}")
    r = client.post("/api/apps/import",
                    files={"file": ("s.zip", buf.getvalue(), "application/zip")},
                    headers=_auth(token))
    assert r.status_code == 400
    assert "not declared" in r.json()["detail"]


def _mutate_manifest(zip_bytes: bytes, change) -> bytes:
    """Rewrite the package with `change(manifest_dict)` applied."""
    def mut(name, data):
        if name == MANIFEST_NAME:
            manifest = json.loads(data)
            change(manifest)
            return name, json.dumps(manifest).encode()
        return name, data
    return _repack(zip_bytes, mut)


def test_import_rejects_non_object_wizard(client, token, exported_zip):
    """A garbage setup_wizard used to import fine and then 500 the setup endpoints."""
    bad = _mutate_manifest(exported_zip, lambda m: m.update(setup_wizard="hello"))
    r = client.post("/api/apps/import",
                    files={"file": ("w.zip", bad, "application/zip")},
                    headers=_auth(token))
    assert r.status_code == 400
    assert "setup_wizard" in r.json()["detail"]


def test_import_rejects_invalid_wizard_schema(client, token, exported_zip):
    bad = _mutate_manifest(exported_zip, lambda m: m.update(
        setup_wizard={"steps": [{"fields": [{"key": "dup"}, {"key": "dup"}]}]}))
    r = client.post("/api/apps/import",
                    files={"file": ("w.zip", bad, "application/zip")},
                    headers=_auth(token))
    assert r.status_code == 400
    assert "duplicate key" in r.json()["detail"]


def test_import_refreshes_stale_vendored_sdk(client, token, exported_zip):
    """A package from an older instance carries that instance's vendored SDK —
    import must replace src/sdk with the CURRENT template's copy so fixed SDK
    bugs (e.g. the config-fetch auth header) aren't reinstalled."""
    stale = b"// ancient SDK without the Authorization header\nexport {}\n"
    rel = "src/sdk/useAppConfig.ts"

    def mut(name, data):
        if name == f"app/{rel}":
            return name, stale
        if name == MANIFEST_NAME:
            m = json.loads(data)
            m["files"][rel] = {"checksum": hashlib.sha256(stale).hexdigest(), "size": len(stale)}
            return name, json.dumps(m).encode()
        return name, data

    r = client.post("/api/apps/import",
                    files={"file": ("stale-sdk.zip", _repack(exported_zip, mut), "application/zip")},
                    headers=_auth(token))
    assert r.status_code == 201, r.text
    new_id = r.json()["app_id"]

    imported = (Path(settings.app_data_dir) / new_id / "draft" / "frontend" / rel).read_text(encoding="utf-8")
    template = (Path(__file__).resolve().parents[2] / "app-template" / rel).read_text(encoding="utf-8")
    assert imported == template
    assert "Authorization" in imported


def test_import_accepts_absent_wizard(client, token, exported_zip):
    ok = _mutate_manifest(exported_zip, lambda m: m.pop("setup_wizard", None))
    r = client.post("/api/apps/import",
                    files={"file": ("w.zip", ok, "application/zip")},
                    headers=_auth(token))
    assert r.status_code == 201, r.text


def test_import_rejects_non_package_zip(client, token):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", b"hello")
    r = client.post("/api/apps/import",
                    files={"file": ("x.zip", buf.getvalue(), "application/zip")},
                    headers=_auth(token))
    assert r.status_code == 400
    assert MANIFEST_NAME in r.json()["detail"]


def test_import_rejects_garbage(client, token):
    r = client.post("/api/apps/import",
                    files={"file": ("x.zip", b"not a zip at all", "application/zip")},
                    headers=_auth(token))
    assert r.status_code == 400
