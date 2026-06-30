"""Team CRUD + membership + effective-group resolution + access enforcement."""
from __future__ import annotations

import json

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..apps.models import App, AppPermission
from ..auth.models import User
from .models import Team, TeamMembership


async def list_teams(db: AsyncSession) -> list[dict]:
    rows = (await db.execute(select(Team).order_by(Team.name))).scalars().all()
    counts = dict((tid, n) for tid, n in (await db.execute(
        select(TeamMembership.team_id, func.count(TeamMembership.id)).group_by(TeamMembership.team_id)
    )).all())
    return [{"id": t.id, "name": t.name, "description": t.description,
             "member_count": int(counts.get(t.id, 0)),
             "created_at": t.created_at.isoformat()} for t in rows]


async def get_team(db: AsyncSession, team_id: str) -> Team | None:
    return (await db.execute(select(Team).where(Team.id == team_id))).scalar_one_or_none()


async def create_team(db: AsyncSession, name: str, description: str) -> Team:
    name = name.strip()
    if not name:
        raise ValueError("Team name is required")
    if (await db.execute(select(Team).where(Team.name == name))).scalar_one_or_none():
        raise ValueError("A team with that name already exists")
    team = Team(name=name, description=description or "")
    db.add(team)
    await db.commit()
    await db.refresh(team)
    return team


async def update_team(db: AsyncSession, team_id: str, name: str | None, description: str | None) -> Team | None:
    team = await get_team(db, team_id)
    if not team:
        return None
    if name is not None:
        team.name = name.strip()
    if description is not None:
        team.description = description
    await db.commit()
    await db.refresh(team)
    return team


async def delete_team(db: AsyncSession, team_id: str) -> bool:
    team = await get_team(db, team_id)
    if not team:
        return False
    await db.execute(delete(TeamMembership).where(TeamMembership.team_id == team_id))
    await db.delete(team)
    await db.commit()
    return True


async def list_members(db: AsyncSession, team_id: str) -> list[dict]:
    rows = (await db.execute(
        select(User.id, User.username, User.display_name, User.role)
        .join(TeamMembership, TeamMembership.user_id == User.id)
        .where(TeamMembership.team_id == team_id)
        .order_by(User.username)
    )).all()
    return [{"user_id": uid, "username": un, "display_name": dn, "role": role}
            for uid, un, dn, role in rows]


async def add_member(db: AsyncSession, team_id: str, user_id: str) -> bool:
    if not await get_team(db, team_id):
        return False
    exists = (await db.execute(select(TeamMembership).where(
        TeamMembership.team_id == team_id, TeamMembership.user_id == user_id))).scalar_one_or_none()
    if exists:
        return True
    db.add(TeamMembership(team_id=team_id, user_id=user_id))
    await db.commit()
    return True


async def remove_member(db: AsyncSession, team_id: str, user_id: str) -> bool:
    await db.execute(delete(TeamMembership).where(
        TeamMembership.team_id == team_id, TeamMembership.user_id == user_id))
    await db.commit()
    return True


async def effective_group_names(db: AsyncSession, user: User) -> set[str]:
    """A user's groups = their AD/IdP groups + the names of teams they belong to."""
    groups: set[str] = set()
    try:
        groups |= set(json.loads(user.ad_groups or "[]"))
    except (json.JSONDecodeError, TypeError):
        pass
    team_names = (await db.execute(
        select(Team.name).join(TeamMembership, TeamMembership.team_id == Team.id)
        .where(TeamMembership.user_id == user.id)
    )).all()
    groups |= {n for (n,) in team_names}
    return groups


async def filter_accessible_apps(db: AsyncSession, user: User, apps: list[App]) -> list[App]:
    """Keep apps the user may access. An app with NO permission records is open
    (backward compatible); otherwise the user must match by id or effective group."""
    if not apps:
        return []
    app_ids = [a.id for a in apps]
    perms = (await db.execute(
        select(AppPermission).where(AppPermission.app_id.in_(app_ids))
    )).scalars().all()
    by_app: dict[str, list[AppPermission]] = {}
    for p in perms:
        by_app.setdefault(p.app_id, []).append(p)

    egroups = await effective_group_names(db, user)
    out = []
    for a in apps:
        plist = by_app.get(a.id)
        if not plist:
            out.append(a)  # open app
            continue
        for p in plist:
            if p.user_id and p.user_id == user.id:
                out.append(a)
                break
            if p.group_name and p.group_name in egroups:
                out.append(a)
                break
    return out


async def can_access_app(db: AsyncSession, user: User, app: App) -> bool:
    return bool(await filter_accessible_apps(db, user, [app]))
