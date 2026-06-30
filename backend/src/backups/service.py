"""Create / list / prune backups, and stage + apply a restore."""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings

logger = logging.getLogger(__name__)

_SKIP = ("node_modules", "dist", ".git")
_MARKER = ".pending-restore"
_ARCNAME = "aihub-backup"


def _db_path() -> Path | None:
    url = settings.database_url
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if url.startswith(prefix):
            return Path(url[len(prefix):]).resolve()
    return None


def backup_dir() -> Path:
    """Config-derived (NOT a platform setting) so restore-at-startup works
    before the DB/settings are available."""
    db = _db_path()
    base = db.parent if db else Path(settings.app_data_dir).resolve()
    return base / "backups"


def _online_backup_sqlite(src: Path, dest: Path) -> None:
    src_conn = sqlite3.connect(str(src))
    dest_conn = sqlite3.connect(str(dest))
    try:
        src_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        src_conn.close()


def _info(path: Path) -> dict:
    stat = path.stat()
    taken_at = None
    # filename: aihub-backup-YYYYmmddTHHMMSSZ.tar.gz
    stem = path.name.replace("aihub-backup-", "").replace(".tar.gz", "")
    taken_at = None
    for fmt in ("%Y%m%dT%H%M%S%fZ", "%Y%m%dT%H%M%SZ"):
        try:
            taken_at = datetime.strptime(stem, fmt).replace(tzinfo=timezone.utc).isoformat()
            break
        except ValueError:
            continue
    if taken_at is None:
        taken_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    return {"name": path.name, "size_bytes": stat.st_size, "taken_at": taken_at}


def create_backup(include_app_data: bool = True) -> dict:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")  # microseconds → unique
    bdir = backup_dir()
    bdir.mkdir(parents=True, exist_ok=True)
    dest = bdir / f"aihub-backup-{ts}.tar.gz"
    staging = bdir / f".staging-{ts}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        src_db = _db_path()
        if src_db and src_db.exists():
            _online_backup_sqlite(src_db, staging / "platform.db")
        if include_app_data:
            dd = Path(settings.app_data_dir).resolve()
            if dd.exists():
                shutil.copytree(dd, staging / "apps", ignore=shutil.ignore_patterns(*_SKIP))
        (staging / "manifest.json").write_text(json.dumps({
            "schema_version": 1, "taken_at": ts, "include_app_data": include_app_data,
        }), encoding="utf-8")
        with tarfile.open(dest, "w:gz") as tar:
            tar.add(staging, arcname=_ARCNAME)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return _info(dest)


def list_backups() -> list[dict]:
    bdir = backup_dir()
    if not bdir.exists():
        return []
    items = [_info(p) for p in bdir.glob("aihub-backup-*.tar.gz") if p.is_file()]
    return sorted(items, key=lambda i: i["name"], reverse=True)


def prune_backups(retention: int) -> int:
    if retention <= 0:
        return 0
    bdir = backup_dir()
    files = sorted(bdir.glob("aihub-backup-*.tar.gz"), key=lambda p: p.name, reverse=True)
    removed = 0
    for old in files[retention:]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def _validate_name(name: str) -> None:
    if "/" in name or "\\" in name or ".." in name or not name.endswith(".tar.gz"):
        raise ValueError("invalid backup name")


def stage_restore(name: str) -> dict:
    """Mark a backup for restore on next startup (safe — never touches the live DB now)."""
    _validate_name(name)
    bdir = backup_dir()
    if not (bdir / name).is_file():
        raise FileNotFoundError(name)
    (bdir / _MARKER).write_text(name, encoding="utf-8")
    return {"staged": True, "backup": name, "restart_required": True}


def pending_restore() -> str | None:
    marker = backup_dir() / _MARKER
    if marker.exists():
        try:
            return marker.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    return None


def apply_pending_restore(db_target: Path | None = None, apps_target: Path | None = None) -> bool:
    """Apply a staged restore. Call at startup BEFORE the DB engine connects.

    Targets default to the live DB + app-data paths; tests pass temp targets so
    they don't clobber their own DB.
    """
    bdir = backup_dir()
    marker = bdir / _MARKER
    if not marker.exists():
        return False
    name = ""
    try:
        name = marker.read_text(encoding="utf-8").strip()
        tarball = bdir / name
        if not tarball.is_file():
            marker.unlink()
            return False
        tmp = bdir / ".restore-tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tarball, "r:gz") as tar:
            try:
                tar.extractall(tmp, filter="data")  # py3.12 path-traversal-safe
            except TypeError:
                tar.extractall(tmp)
        root = tmp / _ARCNAME

        db_dest = db_target or _db_path()
        src_db = root / "platform.db"
        if src_db.is_file() and db_dest:
            db_dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(str(src_db), str(db_dest))

        apps_dest = apps_target or Path(settings.app_data_dir).resolve()
        src_apps = root / "apps"
        if src_apps.is_dir():
            shutil.rmtree(apps_dest, ignore_errors=True)
            apps_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src_apps), str(apps_dest))

        shutil.rmtree(tmp, ignore_errors=True)
        marker.unlink(missing_ok=True)
        logger.info("restore: applied backup %s", name)
        return True
    except Exception:
        logger.exception("restore: failed to apply %s", name)
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            pass
        return False


async def backup_loop():
    """Periodic scheduled backups, gated on the `backup_enabled` setting."""
    import asyncio

    from ..database import async_session
    from ..platform_settings.service import get_all

    await asyncio.sleep(120)  # let startup settle
    while True:
        interval = 24
        try:
            async with async_session() as db:
                cfg = await get_all(db)
            interval = int(cfg.get("backup_interval_hours") or 24)
            if cfg.get("backup_enabled"):
                loop = asyncio.get_event_loop()
                info = await loop.run_in_executor(None, create_backup, True)
                await loop.run_in_executor(None, prune_backups, int(cfg.get("backup_retention") or 7))
                logger.info("scheduled backup created: %s", info.get("name"))
        except Exception:
            logger.exception("backup_loop iteration failed")
        await asyncio.sleep(max(1, interval) * 3600)
