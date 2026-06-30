"""Admin team management endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_role
from ..auth.models import User
from ..database import get_db
from ..secrets.models import AuditLog
from . import service

admin_router = APIRouter()


class TeamIn(BaseModel):
    name: str
    description: str = ""


class TeamUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class MemberIn(BaseModel):
    user_id: str


@admin_router.get("")
async def list_teams(db: AsyncSession = Depends(get_db), _u: User = Depends(require_role("admin"))):
    return await service.list_teams(db)


@admin_router.post("", status_code=201)
async def create_team(body: TeamIn, db: AsyncSession = Depends(get_db),
                      user: User = Depends(require_role("admin"))):
    try:
        team = await service.create_team(db, body.name, body.description)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.add(AuditLog(user_id=user.id, action="team.create", resource_type="team",
                    resource_id=team.id, details=f"Created team '{team.name}'"))
    await db.commit()
    return {"id": team.id, "name": team.name, "description": team.description,
            "member_count": 0, "created_at": team.created_at.isoformat()}


@admin_router.put("/{team_id}")
async def update_team(team_id: str, body: TeamUpdate, db: AsyncSession = Depends(get_db),
                      _u: User = Depends(require_role("admin"))):
    team = await service.update_team(db, team_id, body.name, body.description)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    return {"id": team.id, "name": team.name, "description": team.description}


@admin_router.delete("/{team_id}", status_code=204)
async def delete_team(team_id: str, db: AsyncSession = Depends(get_db),
                      user: User = Depends(require_role("admin"))):
    if not await service.delete_team(db, team_id):
        raise HTTPException(status_code=404, detail="Team not found")
    db.add(AuditLog(user_id=user.id, action="team.delete", resource_type="team",
                    resource_id=team_id, details="Deleted team"))
    await db.commit()


@admin_router.get("/{team_id}/members")
async def list_members(team_id: str, db: AsyncSession = Depends(get_db),
                       _u: User = Depends(require_role("admin"))):
    if not await service.get_team(db, team_id):
        raise HTTPException(status_code=404, detail="Team not found")
    return await service.list_members(db, team_id)


@admin_router.post("/{team_id}/members", status_code=201)
async def add_member(team_id: str, body: MemberIn, db: AsyncSession = Depends(get_db),
                     _u: User = Depends(require_role("admin"))):
    if not await service.add_member(db, team_id, body.user_id):
        raise HTTPException(status_code=404, detail="Team not found")
    return {"ok": True}


@admin_router.delete("/{team_id}/members/{user_id}", status_code=204)
async def remove_member(team_id: str, user_id: str, db: AsyncSession = Depends(get_db),
                        _u: User = Depends(require_role("admin"))):
    await service.remove_member(db, team_id, user_id)
