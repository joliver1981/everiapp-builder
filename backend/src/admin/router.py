from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..auth.dependencies import require_role
from ..auth.models import User
from ..auth.ad_client import ad_client
from .schemas import (
    UserListResponse, RoleUpdateRequest, CreateUserRequest, ResetPasswordRequest,
)
from .service import admin_service
from ..auth.service import auth_service

router = APIRouter()


def _user_list_response(u: User) -> UserListResponse:
    return UserListResponse(
        id=u.id, username=u.username, display_name=u.display_name,
        email=u.email, role=u.role, is_active=u.is_active,
        created_at=u.created_at.isoformat(),
    )


@router.post("/users", response_model=UserListResponse, status_code=201)
async def create_user(
    body: CreateUserRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    """Create a local (username+password) account."""
    if body.role not in ("admin", "developer", "user"):
        raise HTTPException(status_code=400, detail="Invalid role")
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if await auth_service.get_user_by_username(db, body.username):
        raise HTTPException(status_code=409, detail="That username is already taken.")
    new_user = await auth_service.create_local_user(
        db, username=body.username, password=body.password, role=body.role,
        display_name=body.display_name,
    )
    await db.commit()
    return _user_list_response(new_user)


@router.post("/users/{user_id}/reset-password")
async def reset_user_password(
    user_id: str,
    body: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    """Set a new password for a user (admin recovery; the user becomes a local
    account if they weren't already)."""
    if len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    target = await auth_service.get_user_by_id(db, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    await auth_service.set_password(db, target, body.new_password)
    await db.commit()
    return {"ok": True, "username": target.username}


@router.get("/users", response_model=list[UserListResponse])
async def list_users(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    users = await admin_service.list_users(db)
    return [
        UserListResponse(
            id=u.id, username=u.username, display_name=u.display_name,
            email=u.email, role=u.role, is_active=u.is_active,
            created_at=u.created_at.isoformat(),
        )
        for u in users
    ]


@router.put("/users/{user_id}/role")
async def update_user_role(
    user_id: str,
    body: RoleUpdateRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    if body.role not in ("admin", "developer", "user"):
        raise HTTPException(status_code=400, detail="Invalid role")
    updated = await admin_service.update_user_role(db, user_id, body.role)
    if not updated:
        raise HTTPException(status_code=404, detail="User not found")
    return UserListResponse(
        id=updated.id, username=updated.username, display_name=updated.display_name,
        email=updated.email, role=updated.role, is_active=updated.is_active,
        created_at=updated.created_at.isoformat(),
    )


@router.post("/users/{user_id}/toggle-active")
async def toggle_user_active(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    updated = await admin_service.toggle_user_active(db, user_id)
    if not updated:
        raise HTTPException(status_code=404, detail="User not found")
    return UserListResponse(
        id=updated.id, username=updated.username, display_name=updated.display_name,
        email=updated.email, role=updated.role, is_active=updated.is_active,
        created_at=updated.created_at.isoformat(),
    )


# ---- System Info Endpoints ----

@router.get("/system/encryption")
async def encryption_status(
    user: User = Depends(require_role("admin")),
):
    """Return the encryption key source so the admin UI can show warnings."""
    from ..secrets.encryption import encryption_service
    return {"key_source": encryption_service.key_source}


# ---- AD Connection Endpoints ----

@router.post("/ad/test")
async def test_ad_connection(
    user: User = Depends(require_role("admin")),
):
    """Test the Active Directory connection."""
    return ad_client.test_connection()


@router.get("/ad/search")
async def search_ad_users(
    q: str = Query(..., min_length=1),
    user: User = Depends(require_role("admin")),
):
    """Search Active Directory for users."""
    return ad_client.search_users(q)
