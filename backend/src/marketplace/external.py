"""External-marketplace integration: full publish pipeline + remote gallery client.

Publish pipeline (publish_app):
  1. build the app's zip package (packaging service)
  2. optionally boot the app and capture screenshots (out-of-process Playwright)
  3. request upload URLs from the marketplace, PUT package + screenshots
     directly to its storage (bypasses any request-body limits on the
     marketplace host)
  4. POST /api/publish with the full metadata + package checksum
  5. persist the returned slug on the App so re-publishing updates the listing

Remote gallery (browse_remote / install_remote):
  - browse proxies the marketplace's public search API
  - install downloads the package via the marketplace's counting endpoint,
    verifies it, and imports it through the same validated path as a manual
    zip import.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..apps.models import App, AppSetting
from ..auth.models import User
from ..packaging.service import PackageError, packaging_service, slugify
from ..platform_settings.service import get_setting
from ..runtime.manager import runtime_manager
from ..secrets.models import AuditLog

logger = logging.getLogger(__name__)

_SCREENSHOT_TIMEOUT_S = 120
_RUNTIME_START_TIMEOUT_S = 180


class MarketplaceError(Exception):
    """User-facing failure in the external-marketplace flow."""


def _make_client() -> httpx.AsyncClient:
    """Factory for the HTTP client — monkeypatched in tests with a MockTransport."""
    return httpx.AsyncClient(timeout=120.0)


async def _resolve_credentials(db: AsyncSession, *, need_key: bool = True) -> tuple[str, str]:
    url = (await get_setting(db, "marketplace_url") or "").rstrip("/")
    key = await get_setting(db, "marketplace_api_key") or ""
    if not url:
        raise MarketplaceError(
            "Marketplace URL is not configured. An admin can set it under "
            "Platform → Settings → AIHub Marketplace."
        )
    if need_key and not key:
        raise MarketplaceError(
            "Marketplace API key is not configured. Get one from the "
            "marketplace's Developer page and save it under Platform → Settings."
        )
    return url, key


# ---------------------------------------------------------------- screenshots

async def capture_screenshots(app_id: str) -> list[bytes]:
    """Boot the app's draft dev server and capture screenshots out of process.

    Returns PNG bytes (possibly empty on capture failure — screenshots are
    best-effort and must never fail a publish).
    """
    # Frozen (PyInstaller) builds have no python interpreter to spawn the
    # Playwright child with — sys.executable is aihub.exe. Skip gracefully.
    if getattr(sys, "frozen", False):
        logger.warning("screenshot capture unavailable in the packaged build; "
                       "publishing without screenshots")
        return []
    proc = None
    try:
        proc = await runtime_manager.start_app(app_id, "draft")
        deadline = time.monotonic() + _RUNTIME_START_TIMEOUT_S
        port = None
        while time.monotonic() < deadline:
            status = runtime_manager.get_status(app_id)
            if status and status.status == "running" and status.port:
                port = status.port
                break
            if status and status.status == "error":
                logger.warning("screenshot capture: app %s failed to start: %s",
                               app_id, status.error)
                return []
            await asyncio.sleep(2)
        if not port:
            logger.warning("screenshot capture: app %s never reached running", app_id)
            return []

        url = f"http://127.0.0.1:{port}/apps/{app_id}/"
        child = Path(__file__).with_name("screenshot_child.py")
        with tempfile.TemporaryDirectory(prefix="aihub-shots-") as tmp:
            def _run() -> dict:
                creation_flags = 0
                if sys.platform == "win32":
                    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP
                completed = subprocess.run(
                    [sys.executable, str(child), url, tmp],
                    capture_output=True, text=True,
                    timeout=_SCREENSHOT_TIMEOUT_S, creationflags=creation_flags,
                )
                try:
                    return json.loads(completed.stdout.strip().splitlines()[-1])
                except Exception:
                    return {"files": [], "error": completed.stderr[-500:] or "no output"}

            result = await asyncio.get_event_loop().run_in_executor(None, _run)
            if result.get("error"):
                logger.warning("screenshot child error for %s: %s", app_id, result["error"])
            return [Path(f).read_bytes() for f in result.get("files", []) if Path(f).exists()]
    except Exception:
        logger.exception("screenshot capture failed for %s (non-fatal)", app_id)
        return []
    finally:
        if proc is not None:
            try:
                await runtime_manager.stop_app(app_id)
            except Exception:
                pass


# ---------------------------------------------------------------- uploads

async def _upload_via_marketplace(
    client: httpx.AsyncClient, base_url: str, api_key: str,
    *, kind: str, filename: str, slug: str, data: bytes, content_type: str,
) -> str:
    """Request an upload URL and PUT the bytes. Returns the canonical blobUrl."""
    resp = await client.post(
        f"{base_url}/api/publish/upload-url",
        json={"kind": kind, "filename": filename, "slug": slug},
        headers={"X-API-Key": api_key},
    )
    if resp.status_code != 200:
        raise MarketplaceError(
            f"Marketplace refused the {kind} upload request "
            f"({resp.status_code}): {_error_detail(resp)}"
        )
    target = resp.json()
    headers = {**target.get("headers", {}), "Content-Type": content_type}
    put = await client.put(target["uploadUrl"], content=data, headers=headers)
    if put.status_code not in (200, 201):
        raise MarketplaceError(f"Storage upload failed ({put.status_code}) for {filename}")
    return target["blobUrl"]


def _error_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        return body.get("error") or body.get("detail") or resp.text[:200]
    except Exception:
        return resp.text[:200]


# ---------------------------------------------------------------- publish

async def publish_app(
    db: AsyncSession,
    app_id: str,
    user: User,
    *,
    category: str = "general",
    tags: list[str] | None = None,
    short_description: str = "",
    description: str | None = None,
    license: str = "MIT",
    release_notes: str = "",
    setup_instructions: str | None = None,
    version: int | None = None,
    version_semver: str | None = None,
    capture_shots: bool = True,
    marketplace_url: str | None = None,
    marketplace_api_key: str | None = None,
) -> dict:
    """Run the full publish pipeline. Returns {url, slug, version}."""
    from .semver import is_valid_semver
    if marketplace_url and marketplace_api_key:
        base_url, api_key = marketplace_url.rstrip("/"), marketplace_api_key
    else:
        base_url, api_key = await _resolve_credentials(db)

    app = (await db.execute(select(App).where(App.id == app_id))).scalar_one_or_none()
    if not app:
        raise MarketplaceError("App not found")
    if app.status != "published" or app.current_version < 1:
        raise MarketplaceError("Save a version of the app before sending it to the marketplace")

    # Which saved version ships (default: latest). export_app validates the
    # snapshot exists both in the DB and on disk.
    target_version = version or app.current_version

    # The public RELEASE semver is a human choice (bump buttons in the dialog).
    # Falls back to the legacy "{snapshot}.0.0" scheme for older clients that
    # don't send one. Validated so we never ship garbage the marketplace rejects.
    publish_semver = version_semver or f"{target_version}.0.0"
    if not is_valid_semver(publish_semver):
        raise MarketplaceError(f"'{publish_semver}' is not a valid version (expected e.g. 1.2.3)")

    # Persist the listing's setup instructions on the app so re-publishes and
    # the package manifest carry them forward.
    if setup_instructions is not None:
        app.setup_instructions = setup_instructions
    # Persist an edited listing description back onto the app (the marketplace
    # payload reads app.description below).
    if description is not None and description.strip():
        app.description = description

    # 1. Package
    try:
        zip_bytes, filename = await packaging_service.export_app(db, app_id, target_version)
    except (PackageError, LookupError) as e:
        raise MarketplaceError(f"Could not package the app: {e}")
    checksum = hashlib.sha256(zip_bytes).hexdigest()

    slug = app.marketplace_slug or slugify(app.name)

    # 2. Screenshots (best-effort). Capture boots the DRAFT, so only attach
    # screenshots when publishing the latest version — for older versions the
    # draft UI wouldn't match the shipped code.
    shots: list[bytes] = []
    if capture_shots and target_version == app.current_version:
        shots = await capture_screenshots(app_id)

    async with _make_client() as client:
        # 3. Uploads
        package_url = await _upload_via_marketplace(
            client, base_url, api_key,
            kind="package", filename=filename, slug=slug,
            data=zip_bytes, content_type="application/zip",
        )
        screenshot_payload = []
        for i, png in enumerate(shots):
            blob_url = await _upload_via_marketplace(
                client, base_url, api_key,
                kind="media", filename=f"{slug}-screenshot-{i + 1}.png", slug=slug,
                data=png, content_type="image/png",
            )
            screenshot_payload.append({
                "url": blob_url,
                "caption": f"{app.name} — {'desktop' if i == 0 else 'compact'}",
            })

        # 4. Publish
        payload = {
            "name": app.name,
            "slug": slug,
            "shortDescription": short_description or (app.description or app.name)[:300],
            "description": app.description or f"# {app.name}\n\nPublished from AIHub Builder.",
            "category": category,
            "tags": tags or [],
            "license": license,
            "version": publish_semver,
            "releaseNotes": release_notes or f"Version {publish_semver} from AIHub Builder",
            "packageUrl": package_url,
            "packageSize": len(zip_bytes),
            "packageChecksum": checksum,
            "setupWizard": app.setup_wizard,
            "setupInstructions": app.setup_instructions or "",
            "screenshots": screenshot_payload,
        }
        resp = await client.post(
            f"{base_url}/api/publish",
            json=payload,
            headers={"X-API-Key": api_key},
        )
        if resp.status_code not in (200, 201):
            raise MarketplaceError(
                f"Marketplace publish failed ({resp.status_code}): {_error_detail(resp)}"
            )
        data = resp.json()

    # 5. Persist listing identity + the semver we just shipped (seeds the next
    # publish's bump buttons + downgrade guard) + audit.
    returned_slug = data.get("app", {}).get("slug", slug)
    app.marketplace_slug = returned_slug
    app.last_published_version = publish_semver
    db.add(AuditLog(
        user_id=user.id, action="app.marketplace.published",
        resource_type="app", resource_id=app_id,
        details=(f"Published {publish_semver} (snapshot v{target_version}) to {base_url} "
                 f"as '{returned_slug}' ({len(zip_bytes)} bytes, "
                 f"{len(screenshot_payload)} screenshot(s))"),
    ))
    await db.commit()

    return {
        "message": data.get("message", "Published successfully!"),
        "marketplace_url": data.get("app", {}).get("url", f"{base_url}/apps/{returned_slug}"),
        "slug": returned_slug,
        "version": target_version,
        "version_semver": publish_semver,
        "screenshots": len(screenshot_payload),
    }


# ---------------------------------------------------------------- remote gallery

async def remote_published_versions(db: AsyncSession, app_id: str) -> dict:
    """Best-effort list of semvers already on this app's marketplace listing.

    Returns {slug, versions: [str], current: str}. Empty when the app was never
    published or the marketplace is unreachable — this only powers the publish
    dialog's grey-out, so it must never block publishing.
    """
    app = (await db.execute(select(App).where(App.id == app_id))).scalar_one_or_none()
    if not app:
        raise MarketplaceError("App not found")
    slug = app.marketplace_slug or slugify(app.name)
    empty = {"slug": slug, "versions": [], "current": ""}
    try:
        base_url, _ = await _resolve_credentials(db, need_key=False)
    except MarketplaceError:
        return empty
    async with _make_client() as client:
        try:
            resp = await client.get(f"{base_url}/api/apps/{slug}/versions")
        except httpx.HTTPError:
            return empty
    if resp.status_code != 200:
        return empty
    data = resp.json()
    return {
        "slug": slug,
        "versions": [v.get("version") for v in data.get("versions", []) if v.get("version")],
        "current": data.get("currentVersion") or "",
    }


async def browse_remote(
    db: AsyncSession, *, q: str = "", category: str = "",
    sort: str = "popular", page: int = 1,
) -> dict:
    """Proxy the marketplace's public app search (no API key needed)."""
    base_url, _ = await _resolve_credentials(db, need_key=False)
    params: dict = {"sort": sort, "page": page, "limit": 24}
    if q:
        params["q"] = q
    if category:
        params["category"] = category
    async with _make_client() as client:
        try:
            resp = await client.get(f"{base_url}/api/apps", params=params)
        except httpx.HTTPError as e:
            raise MarketplaceError(f"Could not reach the marketplace at {base_url}: {e}")
    if resp.status_code != 200:
        raise MarketplaceError(f"Marketplace returned {resp.status_code}")
    data = resp.json()
    data["marketplace_url"] = base_url
    return data


async def install_remote(
    db: AsyncSession, user_id: str, *, slug: str,
    version: str | None = None, wizard_values: dict | None = None,
) -> App:
    """Download an app package from the marketplace and import it as a new app."""
    base_url, _ = await _resolve_credentials(db, need_key=False)
    params = {"client_id": "aihub-builder"}
    if version:
        params["version"] = version
    async with _make_client() as client:
        try:
            resp = await client.get(
                f"{base_url}/api/apps/{slug}/download",
                params=params, follow_redirects=True,
            )
        except httpx.HTTPError as e:
            raise MarketplaceError(f"Download failed: {e}")
    if resp.status_code != 200:
        raise MarketplaceError(f"Marketplace download failed ({resp.status_code}): {_error_detail(resp)}")

    # import_app re-validates everything (manifest, checksums, path safety).
    try:
        app = await packaging_service.import_app(db, resp.content, user_id)
    except PackageError as e:
        raise MarketplaceError(f"Downloaded package failed validation: {e}")

    # Record provenance + apply setup-wizard answers (mirrors the local
    # marketplace install flow; secrets are encrypted by the shared apply path).
    app.installed_from = f"marketplace:{slug}"
    if wizard_values and app.setup_wizard:
        from ..apps.service import apps_service
        await apps_service.apply_wizard_values(db, app, wizard_values)
    await db.commit()
    await db.refresh(app)
    return app
