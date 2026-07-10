"""A single status snapshot for the admin observability dashboard."""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..apps.models import App
from ..auth.dependencies import require_role
from ..auth.models import User
from ..config import settings
from ..database import get_db
from ..deployments.models import Deployment
from ..secrets.models import AuditLog

router = APIRouter()


def _db_path() -> Path | None:
    url = settings.database_url
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if url.startswith(prefix):
            return Path(url[len(prefix):])
    return None


async def _count(db: AsyncSession, model, *where) -> int:
    return int((await db.execute(select(func.count()).select_from(model).where(*where))).scalar_one() or 0)


@router.get("/status")
async def system_status(db: AsyncSession = Depends(get_db),
                        _u: User = Depends(require_role("admin"))):
    # Uptime (deferred import to avoid an import cycle with main).
    try:
        from .. import main as main_mod
        uptime = round(time.time() - main_mod._startup_time) if main_mod._startup_time else 0
    except Exception:
        uptime = 0

    # Running app processes
    try:
        from ..runtime.manager import runtime_manager
        running_apps = sum(1 for p in runtime_manager._processes.values() if p.status == "running")
    except Exception:
        running_apps = 0

    # DB + disk
    db_path = _db_path()
    db_size = db_path.stat().st_size if (db_path and db_path.exists()) else 0
    disk = {}
    try:
        target = db_path.parent if db_path else Path(settings.app_data_dir)
        target.mkdir(parents=True, exist_ok=True)
        total, used, free = shutil.disk_usage(str(target))
        disk = {"total_bytes": total, "used_bytes": used, "free_bytes": free,
                "percent_used": round(used / total * 100, 1) if total else 0}
    except Exception:
        disk = {}

    counts = {
        "apps": await _count(db, App),
        "apps_published": await _count(db, App, App.status == "published"),
        "users": await _count(db, User),
        "users_active": await _count(db, User, User.is_active == True),  # noqa: E712
        "deployments_running": await _count(db, Deployment, Deployment.status == "running"),
        "audit_logs": await _count(db, AuditLog),
    }

    # Background-loop configuration (what's enabled, not live liveness).
    from ..platform_settings.service import get_all
    cfg = await get_all(db)
    loops = {
        "health_probe": True,  # always on
        "audit_rotation": bool(settings.audit_rotation_enabled),
        "siem_forwarding": bool(cfg.get("siem_enabled")),
        "auto_rollback": bool(cfg.get("auto_rollback_enabled")),
        "scheduled_backups": bool(cfg.get("backup_enabled")),
    }

    # Server-function Python environment — the "why won't this import" screenshot.
    from .. import python_env
    from ..python_packages.models import PythonPackage
    from ..python_packages.service import BUNDLED_PACKAGES
    admin_count = (await db.execute(
        select(func.count()).select_from(PythonPackage))).scalar() or 0
    python_env_info = {
        "python_version": python_env.child_python_version(),
        "pip_available": python_env.pip_cmd() is not None,
        "managed_dir": str(python_env.managed_packages_dir()),
        "bundled_count": len(BUNDLED_PACKAGES),
        "admin_count": admin_count,
    }

    from ..version import PLATFORM_VERSION
    return {
        "version": PLATFORM_VERSION,
        "debug": settings.debug,
        "uptime_seconds": uptime,
        "running_apps": running_apps,
        "database": {"path": str(db_path) if db_path else None, "size_bytes": db_size},
        "disk": disk,
        "counts": counts,
        "background_loops": loops,
        "python_env": python_env_info,
    }
