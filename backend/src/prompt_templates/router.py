"""Prompt-library endpoints: list (builders) + CRUD (admins)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_role
from ..auth.models import User
from ..database import get_db
from . import service
from .schemas import TemplateCreate, TemplateResponse, TemplateUpdate

# Mounted at /api/prompt-templates
router = APIRouter()
# Mounted at /api/admin/prompt-templates
admin_router = APIRouter()


@router.get("", response_model=list[TemplateResponse])
async def list_templates(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("admin", "developer")),
):
    return [TemplateResponse.of(t) for t in await service.list_templates(db)]


@admin_router.post("", response_model=TemplateResponse, status_code=201)
async def create_template(
    body: TemplateCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    return TemplateResponse.of(await service.create(db, body, user.id))


@admin_router.put("/{tid}", response_model=TemplateResponse)
async def update_template(
    tid: str,
    body: TemplateUpdate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("admin")),
):
    t = await service.update(db, tid, body)
    if not t:
        raise HTTPException(status_code=404, detail="Template not found")
    return TemplateResponse.of(t)


@admin_router.delete("/{tid}", status_code=204)
async def delete_template(
    tid: str,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("admin")),
):
    if not await service.delete(db, tid):
        raise HTTPException(status_code=404, detail="Template not found")
