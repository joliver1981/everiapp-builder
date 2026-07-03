"""External-marketplace pipeline via the real HTTP routes.

A fake marketplace (httpx.MockTransport) stands in for the public site +
its blob storage, so these tests cover the builder side end-to-end:
publish (package → upload → listing, slug persistence) and the remote
gallery (browse proxy, download → verify → import installs).
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

import httpx
import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_marketplace_external.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_marketplace_external")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "marketplace-external-test")

from src.config import settings  # noqa: E402
from src.database import init_db  # noqa: E402
from src.main import app  # noqa: E402
from src.marketplace import external  # noqa: E402

MP = "https://mp.test"

# Shared fake-marketplace state
FAKE: dict = {"blobs": {}, "published": []}


def _fake_handler(request: httpx.Request) -> httpx.Response:
    url = request.url
    if url.host == "blobs.test" and request.method == "PUT":
        FAKE["blobs"][f"https://blobs.test{url.path}"] = request.content
        return httpx.Response(201)

    if url.host != "mp.test":
        return httpx.Response(404, json={"error": f"unexpected host {url.host}"})

    if url.path == "/api/publish/upload-url" and request.method == "POST":
        if request.headers.get("X-API-Key") != "aihub_fake_key":
            return httpx.Response(401, json={"error": "Invalid API key."})
        body = json.loads(request.content)
        blob = f"https://blobs.test/{body['kind']}/{body['slug']}/{body['filename']}"
        return httpx.Response(200, json={
            "uploadUrl": blob + "?sas=1", "blobUrl": blob,
            "expiresAt": "2027-01-01T00:00:00Z",
            "headers": {"x-ms-blob-type": "BlockBlob"},
        })

    if url.path == "/api/publish" and request.method == "POST":
        if request.headers.get("X-API-Key") != "aihub_fake_key":
            return httpx.Response(401, json={"error": "Invalid API key."})
        payload = json.loads(request.content)
        FAKE["published"].append(payload)
        return httpx.Response(200, json={
            "message": f"App \"{payload['name']}\" published successfully!",
            "app": {"slug": payload["slug"], "name": payload["name"],
                    "version": payload["version"],
                    "url": f"{MP}/apps/{payload['slug']}"},
        })

    if url.path == "/api/apps" and request.method == "GET":
        q = url.params.get("q", "")
        apps_list = [{
            "id": "remote-1", "slug": p["slug"], "name": p["name"],
            "shortDescription": p["shortDescription"], "iconUrl": None,
            "category": p["category"], "tags": p["tags"],
            "currentVersion": p["version"], "developerName": "Fake Dev",
            "avgRating": "0.00", "reviewCount": 0, "installCount": 3,
            "isFeatured": False, "publishedAt": "2026-06-12T00:00:00Z",
            "setupWizard": p.get("setupWizard"),
        } for p in FAKE["published"]
            if not q or q.lower() in p["name"].lower()]
        return httpx.Response(200, json={
            "apps": apps_list,
            "pagination": {"page": 1, "limit": 24, "total": len(apps_list),
                           "totalPages": 1, "hasMore": False},
        })

    if url.path.startswith("/api/apps/") and url.path.endswith("/versions"):
        slug = url.path.split("/")[3]
        pubs = [p for p in FAKE["published"] if p["slug"] == slug]
        if not pubs:
            return httpx.Response(404, json={"error": "App not found."})
        return httpx.Response(200, json={
            "slug": slug,
            "currentVersion": pubs[-1]["version"],
            "versions": [{"version": p["version"], "releaseNotes": p.get("releaseNotes", ""),
                          "createdAt": "2026-07-02T00:00:00Z"} for p in pubs],
        })

    if url.path.startswith("/api/apps/") and url.path.endswith("/download"):
        slug = url.path.split("/")[3]
        published = [p for p in FAKE["published"] if p["slug"] == slug]
        if not published:
            return httpx.Response(404, json={"error": "App not found."})
        blob = FAKE["blobs"].get(published[-1]["packageUrl"])
        if blob is None:
            return httpx.Response(404, json={"error": "No package."})
        return httpx.Response(200, content=blob,
                              headers={"Content-Type": "application/zip"})

    return httpx.Response(404, json={"error": f"unhandled {request.method} {url.path}"})


@pytest.fixture(scope="module", autouse=True)
def _init():
    asyncio.run(init_db())
    original = external._make_client
    external._make_client = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(_fake_handler))
    yield
    external._make_client = original


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
def configured(client, token):
    r = client.put("/api/admin/settings", json={
        "marketplace_url": MP, "marketplace_api_key": "aihub_fake_key",
    }, headers=_auth(token))
    assert r.status_code == 200, r.text
    return True


WIZARD = {"steps": [{"fields": [{"key": "api_token", "label": "API token",
                                 "type": "secret", "required": True}]}]}


@pytest.fixture(scope="module")
def published_app(client, token):
    r = client.post("/api/apps", json={
        "name": "Remote Pipe App", "description": "pipeline test app",
    }, headers=_auth(token))
    assert r.status_code == 201, r.text
    app_id = r.json()["id"]

    draft = Path(settings.app_data_dir) / app_id / "draft" / "frontend"
    (draft / "src").mkdir(parents=True, exist_ok=True)
    (draft / "src" / "pipe.ts").write_text("export const PIPE = 'external-42'\n")

    r = client.put(f"/api/apps/{app_id}/wizard", json=WIZARD, headers=_auth(token))
    assert r.status_code == 200, r.text
    r = client.post(f"/api/apps/{app_id}/versions", json={"notes": "v1"}, headers=_auth(token))
    assert r.status_code == 201, r.text
    return app_id


def test_publish_requires_configuration(client, token, published_app):
    # No settings configured yet (this test runs before `configured`).
    r = client.post("/api/marketplace/publish-external", json={
        "app_id": published_app, "capture_screenshots": False,
    }, headers=_auth(token))
    assert r.status_code == 400
    assert "not configured" in r.json()["detail"]


def test_full_publish_pipeline(client, token, published_app, configured):
    r = client.post("/api/marketplace/publish-external", json={
        "app_id": published_app,
        "category": "analytics",
        "tags": ["pipeline", "test"],
        "short_description": "A pipeline test app",
        "license": "Apache-2.0",
        "release_notes": "first release",
        "capture_screenshots": False,  # no runtime/Playwright in unit CI
    }, headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["slug"] == "remote-pipe-app"
    assert body["marketplace_url"] == f"{MP}/apps/remote-pipe-app"

    # The fake marketplace received a real package with a matching checksum.
    assert len(FAKE["published"]) == 1
    payload = FAKE["published"][0]
    blob = FAKE["blobs"][payload["packageUrl"]]
    assert payload["packageSize"] == len(blob)
    assert hashlib.sha256(blob).hexdigest() == payload["packageChecksum"]
    assert payload["category"] == "analytics"
    assert payload["license"] == "Apache-2.0"
    assert payload["version"] == "1.0.0"
    assert payload["setupWizard"]["steps"]
    # And the blob is a valid AIHub package.
    zf = zipfile.ZipFile(io.BytesIO(blob))
    manifest = json.loads(zf.read("aihub-app.json"))
    assert manifest["slug"] == "remote-pipe-app"
    assert "src/pipe.ts" in manifest["files"]


def test_republish_targets_same_slug(client, token, published_app, configured):
    # Publish v2, then publish externally again — must reuse the stored slug.
    r = client.post(f"/api/apps/{published_app}/versions", json={"notes": "v2"},
                    headers=_auth(token))
    assert r.status_code == 201, r.text
    r = client.post("/api/marketplace/publish-external", json={
        "app_id": published_app, "capture_screenshots": False,
    }, headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["slug"] == "remote-pipe-app"
    assert FAKE["published"][-1]["version"] == "2.0.0"


def test_explicit_semver_publish_and_tracking(client, token, published_app, configured):
    """An explicit release semver ships as-is and is remembered on the app."""
    r = client.post("/api/marketplace/publish-external", json={
        "app_id": published_app, "version_semver": "1.5.0", "capture_screenshots": False,
    }, headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["version_semver"] == "1.5.0"
    assert FAKE["published"][-1]["version"] == "1.5.0"

    # Persisted so the next publish's bump buttons/guard know the last release.
    r = client.get(f"/api/apps/{published_app}", headers=_auth(token))
    assert r.json()["last_published_version"] == "1.5.0"


def test_invalid_semver_rejected(client, token, published_app, configured):
    for bad in ("1.5", "1.02.0", "v1.0.0", "1.2.3.4", "01.0.0"):
        r = client.post("/api/marketplace/publish-external", json={
            "app_id": published_app, "version_semver": bad, "capture_screenshots": False,
        }, headers=_auth(token))
        assert r.status_code == 400, f"{bad} -> {r.status_code}"
        assert "not a valid version" in r.json()["detail"], bad


def test_semver_falls_back_to_snapshot_scheme(client, token, configured):
    """No version_semver → legacy '{snapshot}.0.0' (backward compatible)."""
    r = client.post("/api/apps", json={"name": "Legacy Semver App"}, headers=_auth(token))
    app_id = r.json()["id"]
    draft = Path(settings.app_data_dir) / app_id / "draft" / "frontend"
    (draft / "src").mkdir(parents=True, exist_ok=True)
    (draft / "src" / "x.ts").write_text("export const X = 1\n")
    r = client.post(f"/api/apps/{app_id}/versions", json={"notes": "v1"}, headers=_auth(token))
    assert r.status_code == 201, r.text
    r = client.post("/api/marketplace/publish-external", json={
        "app_id": app_id, "capture_screenshots": False,
    }, headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["version_semver"] == "1.0.0"


def test_published_versions_greys_out_collisions(client, token, published_app, configured):
    """The dialog reads already-published semvers to disable colliding bumps."""
    r = client.get(f"/api/marketplace/published-versions?app_id={published_app}",
                   headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    # Prior tests published 1.0.0, 2.0.0, and 1.5.0 for this listing.
    assert "1.5.0" in body["versions"] and "2.0.0" in body["versions"]
    assert body["slug"] == "remote-pipe-app"


def test_published_versions_empty_for_unpublished(client, token):
    r = client.post("/api/apps", json={"name": "Never Published"}, headers=_auth(token))
    app_id = r.json()["id"]
    r = client.get(f"/api/marketplace/published-versions?app_id={app_id}", headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["versions"] == []


def test_remote_browse(client, token, configured):
    r = client.get("/api/marketplace/remote?q=pipe", headers=_auth(token))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["marketplace_url"] == MP
    assert data["apps"] and data["apps"][0]["slug"] == "remote-pipe-app"

    r = client.get("/api/marketplace/remote?q=nomatch-xyz", headers=_auth(token))
    assert r.json()["apps"] == []


def test_remote_install_round_trip(client, token, published_app, configured):
    r = client.post("/api/marketplace/remote/install", json={
        "slug": "remote-pipe-app",
        "wizard_values": {"api_token": "tok-123"},
    }, headers=_auth(token))
    assert r.status_code == 200, r.text
    new_id = r.json()["app_id"]
    assert new_id != published_app

    # Imported as a fresh draft with the package files + wizard intact.
    r = client.get(f"/api/apps/{new_id}", headers=_auth(token))
    body = r.json()
    assert body["name"] == "Remote Pipe App"
    assert body["status"] == "draft"
    assert body["installed_from"] == "marketplace:remote-pipe-app"
    assert body["setup_wizard"]["steps"]

    draft = Path(settings.app_data_dir) / new_id / "draft" / "frontend"
    assert (draft / "src" / "pipe.ts").read_text() == "export const PIPE = 'external-42'\n"

    # Wizard values became app settings.
    r = client.get(f"/api/apps/{new_id}/settings", headers=_auth(token))
    assert r.status_code == 200, r.text
    keys = {s["key"] for s in r.json()}
    assert "api_token" in keys


def test_publish_config_endpoint(client, token, configured):
    """The builder's Publish dialog reads this to decide whether to warn upfront.
    It must report configured state without ever returning the secret API key."""
    r = client.get("/api/marketplace/publish-config", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["url_configured"] is True
    assert body["key_configured"] is True
    assert body["configured"] is True
    assert body["marketplace_url"] == MP
    # The secret key value is never exposed.
    assert "marketplace_api_key" not in body
    assert "aihub_fake_key" not in r.text


def test_remote_install_unknown_slug(client, token, configured):
    r = client.post("/api/marketplace/remote/install", json={"slug": "does-not-exist"},
                    headers=_auth(token))
    assert r.status_code == 400


def test_publish_specific_version_with_setup_instructions(client, token, published_app, configured):
    """Version picker: publish v1 while v2 is latest. The v1 snapshot ships,
    setup instructions ride along + persist, and draft-based screenshot capture
    is skipped for a non-latest version (it would show the wrong UI)."""
    r = client.post("/api/marketplace/publish-external", json={
        "app_id": published_app,
        "version": 1,
        "setup_instructions": "1. Get an **API token** from IT",
        "capture_screenshots": True,  # must be skipped: v1 != latest (v2)
    }, headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 1

    payload = FAKE["published"][-1]
    assert payload["version"] == "1.0.0"
    assert payload["setupInstructions"] == "1. Get an **API token** from IT"
    assert payload["screenshots"] == []  # draft capture skipped for old versions

    # The shipped zip really is the v1 snapshot.
    blob = FAKE["blobs"][payload["packageUrl"]]
    manifest = json.loads(zipfile.ZipFile(io.BytesIO(blob)).read("aihub-app.json"))
    assert manifest["version"] == 1
    assert manifest["setup_instructions"] == "1. Get an **API token** from IT"

    # And the instructions persisted on the app for the next publish.
    r = client.get(f"/api/apps/{published_app}", headers=_auth(token))
    assert r.json()["setup_instructions"] == "1. Get an **API token** from IT"


def test_publish_unknown_version_rejected(client, token, published_app, configured):
    r = client.post("/api/marketplace/publish-external", json={
        "app_id": published_app, "version": 99, "capture_screenshots": False,
    }, headers=_auth(token))
    assert r.status_code == 400
    assert "v99" in r.json()["detail"]


def test_setup_instructions_survive_remote_install(client, token, published_app, configured):
    """The manifest carries setup_instructions, so installs restore them."""
    r = client.post("/api/marketplace/remote/install", json={
        "slug": "remote-pipe-app",
    }, headers=_auth(token))
    assert r.status_code == 200, r.text
    new_id = r.json()["app_id"]
    r = client.get(f"/api/apps/{new_id}", headers=_auth(token))
    assert r.json()["setup_instructions"] == "1. Get an **API token** from IT"


def test_wizard_secret_values_encrypted(client, token, configured):
    """Secret-typed wizard answers must be Fernet-encrypted at rest. Before the
    fix they were stored plaintext, which also made /settings/resolved 500
    (it decrypts by type and chokes on plaintext)."""
    r = client.post("/api/marketplace/remote/install", json={
        "slug": "remote-pipe-app",
        "wizard_values": {"api_token": "s3cret-xyz"},
    }, headers=_auth(token))
    assert r.status_code == 200, r.text
    new_id = r.json()["app_id"]

    # Raw stored value is ciphertext, not the plaintext secret.
    r = client.get(f"/api/apps/{new_id}/settings", headers=_auth(token))
    assert r.status_code == 200, r.text
    tok = next(s for s in r.json() if s["key"] == "api_token")
    if tok.get("value"):
        assert tok["value"] != "s3cret-xyz"

    # The resolved endpoint decrypts it back (this 500'd pre-fix).
    r = client.get(f"/api/apps/{new_id}/settings/resolved", headers=_auth(token))
    assert r.status_code == 200, r.text
    assert "s3cret-xyz" in r.text


def test_suggest_metadata_requires_provider(client, token, published_app):
    """Without an AI provider configured, the suggest endpoint fails cleanly."""
    r = client.post("/api/marketplace/suggest-metadata", json={
        "app_id": published_app,
    }, headers=_auth(token))
    assert r.status_code == 400
    assert "No AI provider" in r.json()["detail"]


def test_suggest_metadata(client, token, published_app, monkeypatch):
    """The suggest endpoint drafts listing fields from a (mocked) LLM reply and
    clamps them to what the marketplace publish schema accepts."""
    from types import SimpleNamespace
    from src import llm_compat
    from src.ai_providers.service import ai_provider_service

    async def fake_cfg(db, purpose="generation"):
        return {"provider_type": "anthropic", "model": "claude-test",
                "api_key": "k", "base_url": None}

    reply = json.dumps({
        "short_description": "x" * 400,          # over the 300 cap
        "description": "## Features\n- does things",
        "category": "not-a-category",             # not in the enum
        "tags": [f"tag{i}" for i in range(15)],   # over the 10 cap
        "release_notes": "- did things",
        "setup_instructions": "1. step",
        "suggested_bump": "major",
    })

    async def fake_acompletion(**kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content=f"```json\n{reply}\n```"))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50),
        )

    monkeypatch.setattr(ai_provider_service, "get_default_provider_config", fake_cfg)
    monkeypatch.setattr(llm_compat, "acompletion", fake_acompletion)

    r = client.post("/api/marketplace/suggest-metadata", json={
        "app_id": published_app,
    }, headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["short_description"]) == 300
    assert body["description"] == "## Features\n- does things"
    assert body["category"] == "general"          # invalid → fallback
    assert len(body["tags"]) == 10
    assert body["release_notes"] == "- did things"
    assert body["setup_instructions"] == "1. step"
    assert body["suggested_bump"] == "major"


def test_publish_persists_edited_description(client, token, published_app, configured):
    """An edited listing description is sent to the marketplace AND persisted
    onto the app so the next publish/dialog prefills it."""
    r = client.post("/api/marketplace/publish-external", json={
        "app_id": published_app,
        "version_semver": "9.0.0",
        "description": "# My App\n\nA great listing description with **markdown**.",
        "capture_screenshots": False,
    }, headers=_auth(token))
    assert r.status_code == 200, r.text
    assert FAKE["published"][-1]["description"] == "# My App\n\nA great listing description with **markdown**."
    r = client.get(f"/api/apps/{published_app}", headers=_auth(token))
    assert r.json()["description"] == "# My App\n\nA great listing description with **markdown**."


def test_listing_metadata_persists_for_prefill(client, token, configured):
    """Listing fields (short desc, category, tags, license) are saved on the app
    so the publish dialog prefills them next time instead of resetting."""
    r = client.post("/api/apps", json={"name": "Listing Persist App"}, headers=_auth(token))
    app_id = r.json()["id"]
    draft = Path(settings.app_data_dir) / app_id / "draft" / "frontend"
    (draft / "src").mkdir(parents=True, exist_ok=True)
    (draft / "src" / "x.ts").write_text("export const X = 1\n")
    r = client.post(f"/api/apps/{app_id}/versions", json={"notes": "v1"}, headers=_auth(token))
    assert r.status_code == 201, r.text

    r = client.post("/api/marketplace/publish-external", json={
        "app_id": app_id,
        "version_semver": "1.0.0",
        "short_description": "A crisp one-liner for the gallery.",
        "category": "productivity",
        "tags": ["standup", "scrum"],
        "license": "Apache-2.0",
        "capture_screenshots": False,
    }, headers=_auth(token))
    assert r.status_code == 200, r.text

    listing = client.get(f"/api/apps/{app_id}", headers=_auth(token)).json()["marketplace_listing"]
    assert listing["short_description"] == "A crisp one-liner for the gallery."
    assert listing["category"] == "productivity"
    assert listing["tags"] == ["standup", "scrum"]
    assert listing["license"] == "Apache-2.0"
