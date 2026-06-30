"""HTTP routes for the LLM cost dashboard.

  GET /api/admin/llm-usage/summary?days=30
  GET /api/admin/llm-usage/by-user?days=30
  GET /api/admin/llm-usage/by-app?days=30
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_role
from ..auth.models import User
from ..database import get_db
from . import service as usage

router = APIRouter()


@router.get("/summary")
async def get_summary(
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    _u: User = Depends(require_role("admin")),
):
    s = await usage.summary_last_n_days(db, days)
    return {
        "days": days,
        "total_calls": s.total_calls,
        "total_input_tokens": s.total_input_tokens,
        "total_output_tokens": s.total_output_tokens,
        "total_cost_usd": round(s.total_cost_usd, 4),
        "error_count": s.error_count,
    }


@router.get("/by-user")
async def get_by_user(
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    _u: User = Depends(require_role("admin")),
):
    return {"days": days, "users": await usage.breakdown_by_user(db, days)}


@router.get("/by-app")
async def get_by_app(
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    _u: User = Depends(require_role("admin")),
):
    return {"days": days, "apps": await usage.breakdown_by_app(db, days)}
