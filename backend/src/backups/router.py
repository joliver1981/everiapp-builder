"""Admin backup management: list / create / stage-restore."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_role
from ..auth.models import User
from ..database import get_db
from ..platform_settings.service import get_setting
from ..secrets.models import AuditLog
from . import service

admin_router = APIRouter()


@admin_router.get("")
async def list_backups(_u: User = Depends(require_role("admin"))):
    return {"backups": service.list_backups(), "pending_restore": service.pending_restore()}


@admin_router.post("")
async def create_backup(db: AsyncSession = Depends(get_db),
                        user: User = Depends(require_role("admin"))):
    info = await asyncio.get_event_loop().run_in_executor(None, service.create_backup, True)
    retention = int(await get_setting(db, "backup_retention") or 7)
    await asyncio.get_event_loop().run_in_executor(None, service.prune_backups, retention)
    db.add(AuditLog(user_id=user.id, action="backup.create",
                    resource_type="backup", resource_id=info["name"],
                    details=f"Manual backup {info['name']} ({info['size_bytes']} bytes)"))
    await db.commit()
    return info


@admin_router.post("/{name}/restore")
async def restore_backup(name: str, db: AsyncSession = Depends(get_db),
                         user: User = Depends(require_role("admin"))):
    try:
        result = service.stage_restore(name)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid backup name")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Backup not found")
    db.add(AuditLog(user_id=user.id, action="backup.restore_staged",
                    resource_type="backup", resource_id=name,
                    details=f"Restore staged for {name}; applies on next restart"))
    await db.commit()
    return result
