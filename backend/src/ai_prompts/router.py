"""Admin API: view + override the backend AI prompts, and inspect the generation flow.

Mounted at /api/admin/ai. Every override/reset is audited because a bad system
prompt can break ALL app generation platform-wide.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_role
from ..auth.models import User
from ..database import get_db
from ..secrets.models import AuditLog
from . import registry

admin_router = APIRouter()


class PromptUpdate(BaseModel):
    text: str


async def _one(db: AsyncSession, key: str) -> dict:
    for p in await registry.catalog(db):
        if p["key"] == key:
            return p
    raise HTTPException(status_code=404, detail=f"Unknown prompt '{key}'")


@admin_router.get("/prompts")
async def list_prompts(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    return {"prompts": await registry.catalog(db)}


@admin_router.get("/flow")
async def get_flow(user: User = Depends(require_role("admin"))):
    """The generation pipeline stages + which prompts plug into each (visual panel)."""
    return {"stages": registry.flow()}


@admin_router.put("/prompts/{key}")
async def update_prompt(
    key: str,
    body: PromptUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    try:
        await registry.set_override(db, key, body.text)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown prompt '{key}'")
    db.add(AuditLog(
        user_id=user.id, action="ai_prompt.override", resource_type="ai_prompt",
        resource_id=key, details=f"Overrode prompt '{key}' ({len(body.text)} chars)",
    ))
    await db.commit()
    return {"ok": True, "prompt": await _one(db, key)}


@admin_router.post("/prompts/{key}/reset")
async def reset_prompt(
    key: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    try:
        await registry.clear_override(db, key)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown prompt '{key}'")
    db.add(AuditLog(
        user_id=user.id, action="ai_prompt.reset", resource_type="ai_prompt",
        resource_id=key, details=f"Reset prompt '{key}' to default",
    ))
    await db.commit()
    return {"ok": True, "prompt": await _one(db, key)}
