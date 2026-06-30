"""App package export/import — the portable zip format apps travel in.

This format is the contract shared with the external AIHub Marketplace:

    my-app-v3.zip
    ├── aihub-app.json    # manifest: schema ver, metadata, setup wizard, checksums
    └── app/…             # the version snapshot (no node_modules / dist / .git)

Every file under app/ must appear in the manifest with a matching SHA-256,
and vice versa — import refuses packages where the two disagree, so a
package can't be silently tampered with between export and install.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..apps.models import App, AppVersion
from ..apps.schemas import AppCreate
from ..apps.service import apps_service
from ..config import settings
from ..secrets.models import AuditLog

MANIFEST_NAME = "aihub-app.json"
PACKAGE_KIND = "aihub-app-package"
PACKAGE_SCHEMA = 1
FILES_PREFIX = "app/"

# Heavy/derived dirs never belong in a package (matches the version-snapshot
# ignore list — present for defense in depth against hand-built packages).
_SKIP_DIRS = ("node_modules", "dist", ".git")
# Runtime artifacts that leak into draft/version dirs but aren't app source.
_SKIP_FILES = (".vite-dev.log",)

# Abuse guards for imported archives.
MAX_PACKAGE_BYTES = 100 * 1024 * 1024        # compressed upload cap
MAX_TOTAL_UNCOMPRESSED = 300 * 1024 * 1024   # zip-bomb cap
MAX_FILES = 5000


class PackageError(ValueError):
    """Invalid package or invalid export/import request (maps to HTTP 400)."""


def slugify(name: str) -> str:
    slug = name.lower().replace(" ", "-")
    slug = "".join(c for c in slug if c.isalnum() or c == "-")
    return slug.strip("-") or "app"


def _safe_rel_path(rel: str) -> bool:
    """True if a manifest-relative path is safe to extract under the draft dir."""
    if not rel or rel.startswith("/") or "\\" in rel or ":" in rel:
        return False
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return False
    if any(p in _SKIP_DIRS for p in parts):
        return False
    return True


class PackagingService:
    # ------------------------------------------------------------- export --
    async def export_app(
        self, db: AsyncSession, app_id: str, version: int | None = None
    ) -> tuple[bytes, str]:
        """Build a package zip for a published version. Returns (bytes, filename)."""
        app = (await db.execute(select(App).where(App.id == app_id))).scalar_one_or_none()
        if not app:
            raise LookupError("App not found")

        target_version = version or app.current_version
        if target_version < 1:
            raise PackageError("App has no published versions to export")

        ver = (await db.execute(
            select(AppVersion).where(
                AppVersion.app_id == app_id, AppVersion.version == target_version
            )
        )).scalar_one_or_none()
        if not ver:
            raise PackageError(f"Version v{target_version} not found")

        version_dir = Path(settings.app_data_dir) / app_id / "versions" / f"v{target_version}"
        if not version_dir.exists():
            raise PackageError(f"Version v{target_version} files not found on disk")

        manifest = {
            "schema": PACKAGE_SCHEMA,
            "kind": PACKAGE_KIND,
            "name": app.name,
            "slug": slugify(app.name),
            "description": app.description or "",
            "icon": app.icon,
            "version": target_version,
            "semver": f"{target_version}.0.0",
            "notes": ver.notes or "",
            "setup_wizard": app.setup_wizard,
            "setup_instructions": app.setup_instructions or "",
            "source_app_id": app.id,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "files": {},  # filled while zipping
        }

        zip_bytes = await asyncio.get_event_loop().run_in_executor(
            None, self._build_zip, version_dir, manifest
        )
        filename = f"{manifest['slug']}-v{target_version}.zip"
        return zip_bytes, filename

    def _build_zip(self, version_dir: Path, manifest: dict) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in sorted(version_dir.rglob("*")):
                if not file_path.is_file():
                    continue
                rel_parts = file_path.relative_to(version_dir).parts
                if any(p in _SKIP_DIRS for p in rel_parts):
                    continue
                if rel_parts[-1] in _SKIP_FILES:
                    continue
                rel = "/".join(rel_parts)
                data = file_path.read_bytes()
                manifest["files"][rel] = {
                    "checksum": hashlib.sha256(data).hexdigest(),
                    "size": len(data),
                }
                zf.writestr(FILES_PREFIX + rel, data)
            # Manifest goes in last, once the file table is complete.
            zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2))
        return buf.getvalue()

    # ------------------------------------------------------------- import --
    async def import_app(self, db: AsyncSession, data: bytes, user_id: str) -> App:
        """Validate a package zip and create a new draft app from it."""
        if len(data) > MAX_PACKAGE_BYTES:
            raise PackageError(
                f"Package exceeds the {MAX_PACKAGE_BYTES // (1024 * 1024)}MB limit"
            )

        manifest, files = await asyncio.get_event_loop().run_in_executor(
            None, self._parse_and_verify, data
        )

        app = await apps_service.create_app(
            db,
            AppCreate(
                name=manifest.get("name") or "Imported app",
                description=manifest.get("description") or "",
                icon=manifest.get("icon") or "app-window",
            ),
            user_id,
        )

        draft_dir = Path(settings.app_data_dir) / app.id / "draft" / "frontend"

        def _write_files():
            # Replace the template scaffold with the package contents.
            if draft_dir.exists():
                shutil.rmtree(draft_dir)
            for rel, content in files.items():
                dest = draft_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(content)

        await asyncio.get_event_loop().run_in_executor(None, _write_files)

        app.setup_wizard = manifest.get("setup_wizard")
        app.setup_instructions = str(manifest.get("setup_instructions") or "")
        db.add(AuditLog(
            user_id=user_id, action="app.import",
            resource_type="app", resource_id=app.id,
            details=(
                f"Imported package '{manifest.get('slug')}' "
                f"v{manifest.get('version')} ({len(files)} files)"
            ),
        ))
        await db.commit()
        await db.refresh(app)
        return app

    def _parse_and_verify(self, data: bytes) -> tuple[dict, dict[str, bytes]]:
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise PackageError("Not a valid zip file")

        with zf:
            names = set(zf.namelist())
            if MANIFEST_NAME not in names:
                raise PackageError(f"Package is missing {MANIFEST_NAME}")
            try:
                manifest = json.loads(zf.read(MANIFEST_NAME))
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise PackageError(f"{MANIFEST_NAME} is not valid JSON")

            if manifest.get("kind") != PACKAGE_KIND:
                raise PackageError("Not an AIHub app package")
            if manifest.get("schema") != PACKAGE_SCHEMA:
                raise PackageError(
                    f"Unsupported package schema {manifest.get('schema')!r} "
                    f"(this builder supports schema {PACKAGE_SCHEMA})"
                )
            declared = manifest.get("files")
            if not isinstance(declared, dict) or not declared:
                raise PackageError("Manifest declares no files")
            if len(declared) > MAX_FILES:
                raise PackageError(f"Package exceeds the {MAX_FILES}-file limit")

            total_uncompressed = sum(i.file_size for i in zf.infolist())
            if total_uncompressed > MAX_TOTAL_UNCOMPRESSED:
                raise PackageError("Package expands past the uncompressed size limit")

            files: dict[str, bytes] = {}
            for info in zf.infolist():
                if info.is_dir() or info.filename == MANIFEST_NAME:
                    continue
                if not info.filename.startswith(FILES_PREFIX):
                    raise PackageError(f"Unexpected entry outside {FILES_PREFIX}: {info.filename}")
                rel = info.filename[len(FILES_PREFIX):]
                if not _safe_rel_path(rel):
                    raise PackageError(f"Unsafe file path in package: {info.filename}")
                if rel not in declared:
                    raise PackageError(f"File not declared in manifest: {rel}")
                content = zf.read(info)
                if hashlib.sha256(content).hexdigest() != declared[rel].get("checksum"):
                    raise PackageError(f"Checksum mismatch for {rel} — package corrupted or tampered")
                files[rel] = content

            missing = set(declared) - set(files)
            if missing:
                raise PackageError(f"Manifest declares missing file(s): {sorted(missing)[:5]}")

        return manifest, files


packaging_service = PackagingService()
