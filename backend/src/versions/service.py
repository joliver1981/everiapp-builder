import asyncio
import difflib
import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ..apps.models import App, AppVersion
from ..config import settings

# Directories we deliberately never include in a published version snapshot.
# node_modules is reproducible from package.json (and easily 200+MB),
# dist is a build artifact, .git would be enormous and meaningless here.
_SKIP_DIRS_FOR_SNAPSHOT = ("node_modules", "dist", ".git")

# Diff limits: don't try to decode/diff files bigger than this (treated as
# binary), and truncate any single file's unified diff past this many chars.
_DIFF_MAX_BYTES = 1_000_000
_DIFF_TRUNCATE_CHARS = 200_000


class VersionsService:
    async def list_versions(self, db: AsyncSession, app_id: str) -> list[AppVersion]:
        result = await db.execute(
            select(AppVersion)
            .where(AppVersion.app_id == app_id)
            .order_by(AppVersion.version.desc())
        )
        return list(result.scalars().all())

    async def publish(self, db: AsyncSession, app_id: str, user_id: str, notes: str = "") -> AppVersion:
        """Create a new immutable version snapshot."""
        # Get app
        result = await db.execute(select(App).where(App.id == app_id))
        app = result.scalar_one_or_none()
        if not app:
            raise ValueError("App not found")

        new_version = app.current_version + 1

        # Copy draft to version directory
        draft_dir = Path(settings.app_data_dir) / app_id / "draft" / "frontend"
        version_dir = Path(settings.app_data_dir) / app_id / "versions" / f"v{new_version}"

        if draft_dir.exists():
            # Run the copy off the event loop so the HTTP request doesn't block
            # while shutil.copytree walks the tree. Also skip the heavy dirs
            # (node_modules etc.) — they're reproducible and would make publish
            # take 10s+ for a typical app.
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: shutil.copytree(
                    draft_dir, version_dir, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(*_SKIP_DIRS_FOR_SNAPSHOT),
                ),
            )

        # Generate manifest with file checksums (also off the event loop)
        manifest = await asyncio.get_event_loop().run_in_executor(
            None, self._generate_manifest, version_dir,
        )

        # Create version record
        version = AppVersion(
            app_id=app_id,
            version=new_version,
            notes=notes,
            published_by=user_id,
            manifest=manifest,
        )
        db.add(version)

        # Update app
        app.current_version = new_version
        app.status = "published"
        app.updated_at = datetime.now(timezone.utc)

        await db.commit()
        await db.refresh(version)
        return version

    async def rollback(self, db: AsyncSession, app_id: str, target_version: int, user_id: str) -> AppVersion:
        """Rollback to a previous version by copying it to draft and creating a new version entry."""
        version_dir = Path(settings.app_data_dir) / app_id / "versions" / f"v{target_version}"
        if not version_dir.exists():
            raise ValueError(f"Version v{target_version} not found on disk")

        draft_dir = Path(settings.app_data_dir) / app_id / "draft" / "frontend"

        # Clear draft and copy version files — off the event loop, and skipping
        # the same heavy dirs in case the source version was made before the
        # snapshot ignore-filter was added (older versions may still contain
        # node_modules in their on-disk tree).
        def _do_swap():
            if draft_dir.exists():
                shutil.rmtree(draft_dir)
            shutil.copytree(
                version_dir, draft_dir,
                ignore=shutil.ignore_patterns(*_SKIP_DIRS_FOR_SNAPSHOT),
            )
        await asyncio.get_event_loop().run_in_executor(None, _do_swap)

        # Create a new publish from the rolled-back state
        return await self.publish(
            db, app_id, user_id,
            notes=f"Rollback to v{target_version}"
        )

    async def get_version_files(self, app_id: str, version: int) -> list[dict]:
        """Get file tree for a specific version."""
        version_dir = Path(settings.app_data_dir) / app_id / "versions" / f"v{version}"
        if not version_dir.exists():
            return []
        return self._build_tree(version_dir, version_dir)

    # --- Diff between two refs (version numbers or "draft") ----------------
    def _ref_dir(self, app_id: str, ref: str) -> Path:
        base = Path(settings.app_data_dir) / app_id
        if str(ref) == "draft":
            return base / "draft" / "frontend"
        return base / "versions" / f"v{int(ref)}"

    def _read_files_flat(self, root: Path) -> dict[str, dict]:
        """Map relative posix path -> {sha, text|None}. Heavy/binary files get
        text=None (still hashed, so we can report 'changed' without a diff)."""
        out: dict[str, dict] = {}
        if not root.exists():
            return out
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            rel_parts = p.relative_to(root).parts
            if any(part in _SKIP_DIRS_FOR_SNAPSHOT for part in rel_parts):
                continue
            try:
                data = p.read_bytes()
            except OSError:
                continue
            text: str | None = None
            if len(data) <= _DIFF_MAX_BYTES:
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    text = None
            out[str(p.relative_to(root)).replace("\\", "/")] = {
                "sha": hashlib.sha256(data).hexdigest(),
                "text": text,
            }
        return out

    async def diff_versions(self, app_id: str, from_ref: str, to_ref: str) -> dict:
        """Unified diff of every changed file between two refs.

        Each ref is a version number ("1", "2", ...) or the literal "draft".
        Returns added / removed / modified files with per-file unified diffs.
        """
        from_dir = self._ref_dir(app_id, from_ref)
        to_dir = self._ref_dir(app_id, to_ref)
        if not from_dir.exists():
            raise ValueError(f"Version '{from_ref}' not found")
        if not to_dir.exists():
            raise ValueError(f"Version '{to_ref}' not found")

        loop = asyncio.get_event_loop()
        a = await loop.run_in_executor(None, self._read_files_flat, from_dir)
        b = await loop.run_in_executor(None, self._read_files_flat, to_dir)

        files: list[dict] = []
        summary = {"added": 0, "removed": 0, "modified": 0}

        for path in sorted(set(a) | set(b)):
            fa, fb = a.get(path), b.get(path)
            if fa and not fb:
                summary["removed"] += 1
                files.append({"path": path, "status": "removed", "binary": fa["text"] is None,
                              "additions": 0,
                              "deletions": len(fa["text"].splitlines()) if fa["text"] else 0,
                              "diff": ""})
            elif fb and not fa:
                summary["added"] += 1
                files.append({"path": path, "status": "added", "binary": fb["text"] is None,
                              "additions": len(fb["text"].splitlines()) if fb["text"] else 0,
                              "deletions": 0, "diff": ""})
            else:
                if fa["sha"] == fb["sha"]:
                    continue  # unchanged
                summary["modified"] += 1
                if fa["text"] is None or fb["text"] is None:
                    files.append({"path": path, "status": "modified", "binary": True,
                                  "additions": 0, "deletions": 0, "diff": ""})
                    continue
                diff_lines = list(difflib.unified_diff(
                    fa["text"].splitlines(), fb["text"].splitlines(),
                    fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="",
                ))
                adds = sum(1 for ln in diff_lines if ln.startswith("+") and not ln.startswith("+++"))
                dels = sum(1 for ln in diff_lines if ln.startswith("-") and not ln.startswith("---"))
                diff_text = "\n".join(diff_lines)
                truncated = False
                if len(diff_text) > _DIFF_TRUNCATE_CHARS:
                    diff_text = diff_text[:_DIFF_TRUNCATE_CHARS] + "\n… (diff truncated)"
                    truncated = True
                files.append({"path": path, "status": "modified", "binary": False,
                              "additions": adds, "deletions": dels,
                              "diff": diff_text, "truncated": truncated})

        return {"app_id": app_id, "from": str(from_ref), "to": str(to_ref),
                "summary": summary, "files": files}

    def _generate_manifest(self, directory: Path) -> dict:
        """Generate checksums for all files in a directory."""
        manifest = {"files": {}}
        if not directory.exists():
            return manifest

        for file_path in directory.rglob("*"):
            if file_path.is_file() and "node_modules" not in str(file_path):
                rel_path = str(file_path.relative_to(directory)).replace("\\", "/")
                content = file_path.read_bytes()
                manifest["files"][rel_path] = {
                    "checksum": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                }

        return manifest

    def _build_tree(self, root: Path, base: Path) -> list[dict]:
        entries = []
        for item in sorted(root.iterdir()):
            if item.name in ('node_modules', '.git', 'dist'):
                continue
            rel_path = str(item.relative_to(base)).replace('\\', '/')
            if item.is_dir():
                entries.append({
                    "name": item.name, "path": rel_path,
                    "type": "directory", "children": self._build_tree(item, base),
                })
            else:
                entries.append({"name": item.name, "path": rel_path, "type": "file", "children": []})
        return entries


versions_service = VersionsService()
