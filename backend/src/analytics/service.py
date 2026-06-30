"""Event recording + aggregation for per-app analytics."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..apps.models import App
from ..llm_usage.models import LLMUsage
from .models import AppEvent

# Cap how much metadata we keep per event so a noisy client can't bloat the DB.
_MAX_META_KEYS = 20


async def record_event(db: AsyncSession, app_id: str, user_id: str | None,
                       event_type: str, metadata: dict | None = None) -> AppEvent:
    meta = None
    if isinstance(metadata, dict) and metadata:
        meta = {str(k): metadata[k] for k in list(metadata)[:_MAX_META_KEYS]}
    ev = AppEvent(app_id=app_id, user_id=user_id,
                  event_type=(event_type or "launch")[:40], event_metadata=meta)
    db.add(ev)
    await db.commit()
    await db.refresh(ev)
    return ev


def _since(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 365)))


async def app_analytics(db: AsyncSession, app_id: str, days: int = 30) -> dict:
    since = _since(days)

    total = (await db.execute(
        select(func.count(AppEvent.id)).where(
            AppEvent.app_id == app_id, AppEvent.created_at >= since)
    )).scalar_one()

    unique_users = (await db.execute(
        select(func.count(func.distinct(AppEvent.user_id))).where(
            AppEvent.app_id == app_id, AppEvent.created_at >= since,
            AppEvent.user_id.is_not(None))
    )).scalar_one()

    by_type_rows = (await db.execute(
        select(AppEvent.event_type, func.count(AppEvent.id))
        .where(AppEvent.app_id == app_id, AppEvent.created_at >= since)
        .group_by(AppEvent.event_type)
    )).all()

    day_expr = func.strftime("%Y-%m-%d", AppEvent.created_at)
    by_day_rows = (await db.execute(
        select(day_expr.label("day"), func.count(AppEvent.id))
        .where(AppEvent.app_id == app_id, AppEvent.created_at >= since)
        .group_by("day").order_by("day")
    )).all()

    llm_cost = float((await db.execute(
        select(func.coalesce(func.sum(LLMUsage.cost_usd), 0.0))
        .where(LLMUsage.app_id == app_id, LLMUsage.created_at >= since)
    )).scalar_one() or 0.0)

    return {
        "app_id": app_id,
        "days": days,
        "total_events": int(total or 0),
        "unique_users": int(unique_users or 0),
        "by_type": {t: int(c) for t, c in by_type_rows},
        "by_day": [{"day": d, "count": int(c)} for d, c in by_day_rows],
        "llm_cost_usd": round(llm_cost, 4),
    }


async def top_apps(db: AsyncSession, days: int = 30, limit: int = 10) -> list[dict]:
    since = _since(days)
    rows = (await db.execute(
        select(
            AppEvent.app_id,
            func.count(AppEvent.id).label("events"),
            func.count(func.distinct(AppEvent.user_id)).label("users"),
        )
        .where(AppEvent.created_at >= since)
        .group_by(AppEvent.app_id)
        .order_by(func.count(AppEvent.id).desc())
        .limit(max(1, min(limit, 100)))
    )).all()

    # Resolve names in one query.
    app_ids = [r[0] for r in rows]
    names: dict[str, str] = {}
    if app_ids:
        for aid, name in (await db.execute(
            select(App.id, App.name).where(App.id.in_(app_ids))
        )).all():
            names[aid] = name

    return [
        {"app_id": aid, "name": names.get(aid, "(deleted)"),
         "events": int(events), "unique_users": int(users)}
        for aid, events, users in rows
    ]
