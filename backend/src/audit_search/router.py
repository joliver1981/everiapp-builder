"""Search/filter the audit log for the admin observability UI."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_role
from ..auth.models import User
from ..database import get_db
from ..secrets.models import AuditLog

router = APIRouter()


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@router.get("/audit-logs")
async def search_audit_logs(
    db: AsyncSession = Depends(get_db),
    _u: User = Depends(require_role("admin")),
    q: str | None = Query(None, description="free text in action/details/resource_id"),
    user_id: str | None = None,
    action: str | None = Query(None, description="action prefix, e.g. 'app.publish'"),
    resource_type: str | None = None,
    start: str | None = Query(None, description="ISO timestamp lower bound"),
    end: str | None = Query(None, description="ISO timestamp upper bound"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    filters = []
    if user_id:
        filters.append(AuditLog.user_id == user_id)
    if action:
        filters.append(AuditLog.action.like(f"{action}%"))
    if resource_type:
        filters.append(AuditLog.resource_type == resource_type)
    if (sdt := _parse_dt(start)) is not None:
        filters.append(AuditLog.created_at >= sdt)
    if (edt := _parse_dt(end)) is not None:
        filters.append(AuditLog.created_at <= edt)
    if q:
        like = f"%{q}%"
        filters.append(or_(
            AuditLog.action.like(like),
            AuditLog.details.like(like),
            AuditLog.resource_id.like(like),
        ))

    total = (await db.execute(
        select(func.count(AuditLog.id)).where(*filters)
    )).scalar_one()

    rows = (await db.execute(
        select(AuditLog).where(*filters)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit).offset(offset)
    )).scalars().all()

    # Resolve usernames in one query.
    uids = {r.user_id for r in rows if r.user_id}
    names: dict[str, str] = {}
    if uids:
        for uid, uname in (await db.execute(
            select(User.id, User.username).where(User.id.in_(uids))
        )).all():
            names[uid] = uname

    return {
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "id": r.id,
                "user_id": r.user_id,
                "username": names.get(r.user_id),
                "action": r.action,
                "resource_type": r.resource_type,
                "resource_id": r.resource_id,
                "details": r.details,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


@router.get("/audit-logs/actions")
async def list_audit_actions(
    db: AsyncSession = Depends(get_db),
    _u: User = Depends(require_role("admin")),
):
    """Distinct action values, for a filter dropdown."""
    rows = (await db.execute(
        select(AuditLog.action, func.count(AuditLog.id))
        .group_by(AuditLog.action).order_by(func.count(AuditLog.id).desc())
    )).all()
    return [{"action": a, "count": int(c)} for a, c in rows]
