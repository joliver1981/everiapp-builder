"""Read API for app-generation traces (builder feature — admin + developer)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_role
from ..auth.models import User
from ..database import get_db
from . import service

router = APIRouter()


@router.get("/{app_id}/traces")
async def list_traces(
    app_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    return {"traces": await service.list_traces(db, app_id)}


@router.get("/{app_id}/traces/{trace_id}")
async def get_trace(
    app_id: str,
    trace_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    t = await service.get_trace(db, trace_id)
    if not t or t["app_id"] != app_id:
        raise HTTPException(status_code=404, detail="Trace not found")
    return t
