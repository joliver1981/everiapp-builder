"""Analytics endpoints: event recording (any user) + aggregates (admin/dev)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user, require_role
from ..auth.models import User
from ..database import get_db
from . import service

# Mounted at /api/apps
router = APIRouter()
# Mounted at /api/admin
admin_router = APIRouter()


class EventIn(BaseModel):
    event_type: str = "launch"
    metadata: dict | None = None


@router.post("/{app_id}/events", status_code=204)
async def record_event(
    app_id: str,
    body: EventIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Record a usage event (launch/view/custom). Fire-and-forget from clients."""
    await service.record_event(db, app_id, user.id, body.event_type, body.metadata)


@admin_router.get("/apps/{app_id}/analytics")
async def app_analytics(
    app_id: str,
    days: int = 30,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("admin", "developer")),
):
    return await service.app_analytics(db, app_id, days)


@admin_router.get("/analytics/top-apps")
async def top_apps(
    days: int = 30,
    limit: int = 10,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("admin")),
):
    return await service.top_apps(db, days, limit)
