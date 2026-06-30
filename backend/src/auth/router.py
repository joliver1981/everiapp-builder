import json
from fastapi import APIRouter, Depends, HTTPException, status, Response, Request
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from .schemas import (
    LoginRequest, LoginResponse, UserResponse,
    BootstrapAdminRequest, ChangePasswordRequest,
)
from .service import auth_service
from .passwords import verify_password
from .cookies import REFRESH_COOKIE, set_refresh_cookie, clear_refresh_cookie
from .dependencies import get_current_user
from .models import User

router = APIRouter()


def _user_response(user: User) -> UserResponse:
    groups = json.loads(user.ad_groups) if user.ad_groups else []
    return UserResponse(
        id=user.id, username=user.username, display_name=user.display_name,
        email=user.email, role=user.role, groups=groups,
        created_at=user.created_at.isoformat(),
    )


@router.post("/bootstrap-admin", response_model=LoginResponse)
async def bootstrap_admin(body: BootstrapAdminRequest, request: Request,
                          response: Response, db: AsyncSession = Depends(get_db)):
    """First-run only: create the initial administrator account and sign in.

    Refuses once ANY admin exists, so it can't be used to hijack a configured
    instance. This is the front door for a fresh install (there are no shipped
    default credentials in a production build)."""
    if await auth_service.admin_exists(db):
        raise HTTPException(status_code=409, detail="An administrator account already exists.")
    if await auth_service.get_user_by_username(db, body.username):
        raise HTTPException(status_code=409, detail="That username is already taken.")

    user = await auth_service.create_local_user(
        db, username=body.username, password=body.password, role="admin",
        display_name=body.username,
    )
    access_token, refresh_value = await auth_service.issue_session(db, user)
    set_refresh_cookie(request, response, refresh_value)
    return LoginResponse(access_token=access_token, user=_user_response(user))


@router.post("/change-password")
async def change_password(body: ChangePasswordRequest,
                          user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    """Change your own password (local accounts only)."""
    if not user.password_hash:
        raise HTTPException(
            status_code=400,
            detail="Your account does not use a local password (it's managed by an identity provider).",
        )
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    await auth_service.set_password(db, user, body.new_password)
    await db.commit()
    return {"ok": True}


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    result = await auth_service.authenticate(db, body.username, body.password)
    if not result:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    user, access_token, refresh_value = result

    # Set refresh token as a hardened httpOnly cookie (Secure auto-detected from scheme)
    set_refresh_cookie(request, response, refresh_value)

    return LoginResponse(access_token=access_token, user=_user_response(user))


@router.post("/refresh")
async def refresh(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    refresh_value = request.cookies.get(REFRESH_COOKIE)
    if not refresh_value:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No refresh token")

    result = await auth_service.refresh_access_token(db, refresh_value)
    if not result:
        clear_refresh_cookie(response)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

    new_access, new_refresh = result

    set_refresh_cookie(request, response, new_refresh)

    return {"access_token": new_access, "token_type": "bearer"}


@router.post("/logout")
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    refresh_value = request.cookies.get(REFRESH_COOKIE)
    if refresh_value:
        await auth_service.revoke_refresh_token(db, refresh_value)
    clear_refresh_cookie(response)
    return {"ok": True}


@router.get("/me")
async def get_me(user: User = Depends(get_current_user)):
    groups = json.loads(user.ad_groups) if user.ad_groups else []
    return {
        "user": UserResponse(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            email=user.email,
            role=user.role,
            groups=groups,
            created_at=user.created_at.isoformat(),
        )
    }
