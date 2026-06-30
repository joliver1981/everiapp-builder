"""Admin SIEM endpoints: status, manual flush, connectivity test."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_role
from ..auth.models import User
from ..database import get_db
from . import forwarder

# Mounted at /api/admin/siem
admin_router = APIRouter()


@admin_router.get("/status")
async def siem_status(db: AsyncSession = Depends(get_db),
                      _u: User = Depends(require_role("admin"))):
    return await forwarder.status(db)


@admin_router.post("/flush")
async def siem_flush(db: AsyncSession = Depends(get_db),
                     _u: User = Depends(require_role("admin"))):
    """Forward any pending audit events right now (otherwise the loop handles it)."""
    try:
        return await forwarder.flush_once(db)
    except forwarder.SiemError as e:
        raise HTTPException(status_code=502, detail=str(e))


@admin_router.post("/test")
async def siem_test(db: AsyncSession = Depends(get_db),
                    _u: User = Depends(require_role("admin"))):
    """Send a single synthetic event to verify connectivity (cursor untouched)."""
    event = [{
        "id": "siem-test",
        "user_id": "system",
        "action": "siem.test",
        "resource_type": "siem",
        "resource_id": "test",
        "details": "AIHub SIEM connectivity test",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": "aihub",
    }]
    try:
        await forwarder.push_events(db, event)
    except forwarder.SiemError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:  # transport/network error
        raise HTTPException(status_code=502, detail=f"SIEM test failed: {e}")
    return {"ok": True}
