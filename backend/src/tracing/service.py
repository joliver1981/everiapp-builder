"""Span queries + retention.

Phase 1 exposes metadata only — encrypted payloads stay at rest until the
Phase 2 trace viewer adds an audited decrypt path.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AISpan

logger = logging.getLogger(__name__)

_SWEEP_INTERVAL_SECONDS = 6 * 3600  # 4x daily; the sweep itself is cheap


async def list_spans(db: AsyncSession, app_id: str, limit: int = 200) -> list[dict]:
    result = await db.execute(
        select(AISpan).where(AISpan.app_id == app_id)
        # Clamp both ends: SQLite treats LIMIT -1 as "no limit".
        .order_by(AISpan.created_at.desc()).limit(max(1, min(limit, 1000)))
    )
    return [_to_dict(s) for s in result.scalars().all()]


def _to_dict(s: AISpan) -> dict:
    return {
        "id": s.id,
        "trace_id": s.trace_id,
        "parent_span_id": s.parent_span_id,
        "app_id": s.app_id,
        "user_id": s.user_id,
        "kind": s.kind,
        "purpose": s.purpose,
        "name": s.name,
        "provider_type": s.provider_type,
        "model": s.model,
        "status": s.status,
        "error": s.error,
        # Presence flags only — never the payloads themselves here.
        "has_prompt": s.prompt_ct is not None,
        "has_response": s.response_ct is not None,
        "capture_level": s.capture_level,
        "input_tokens": s.input_tokens,
        "output_tokens": s.output_tokens,
        "cost_usd": s.cost_usd,
        "latency_ms": s.latency_ms,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


async def retention_sweep(db: AsyncSession) -> int:
    """Delete spans older than trace_retention_days. Returns rows deleted."""
    from ..platform_settings.service import get_setting

    days = await get_setting(db, "trace_retention_days")
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 14
    if days <= 0:  # 0 = keep forever (explicit admin choice)
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = await db.execute(delete(AISpan).where(AISpan.created_at < cutoff))
    await db.commit()
    return result.rowcount or 0


async def retention_loop() -> None:
    """Background task (started in lifespan) — same shape as backup_loop."""
    from ..database import async_session

    while True:
        try:
            async with async_session() as db:
                deleted = await retention_sweep(db)
            if deleted:
                logger.info("trace retention sweep deleted %d spans", deleted)
        except Exception:
            logger.exception("trace retention sweep failed (non-fatal)")
        await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
