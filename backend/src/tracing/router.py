"""Span read API — metadata only until the Phase 2 viewer adds an audited
payload-decrypt path."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user
from ..auth.models import User
from ..database import get_db
from ..apps.service import apps_service
from .service import list_spans

router = APIRouter()


@router.get("/{app_id}/spans")
async def get_app_spans(
    app_id: str,
    limit: int = Query(200, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    app = await apps_service.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="App not found")
    # Same audience as the builder: the app's developers and admins.
    if user.role not in ("admin", "developer"):
        raise HTTPException(status_code=403, detail="Developer or admin role required")
    return await list_spans(db, app_id, limit=limit)
