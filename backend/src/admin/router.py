from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..auth.dependencies import require_role
from ..auth.models import User
from ..auth.ad_client import ad_client
from .schemas import (
    UserListResponse, RoleUpdateRequest, CreateUserRequest, ResetPasswordRequest,
    ProvisionADUserRequest,
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
#
# These back the Users & Roles page's "Active Directory Connection" panel.
# They MUST use the DB-configured LDAP identity provider (Admin → Platform →
# Authentication) when one is enabled — a production install configures LDAP
# there, not via env vars. The env-based ad_client is the DEV mock only; it
# used to be called unconditionally here, so a fully configured production
# install answered "Mock mode — no real AD connection needed" and searched
# three fake users. Field-reported on a real install.


async def _enabled_ldap_provider(db: AsyncSession):
    """(config_row, LdapAuthProvider) for the first enabled LDAP provider, or None."""
    import json as _json
    from ..auth.providers.chain import get_enabled_providers
    from ..auth.providers.ldap_provider import LDAP3_AVAILABLE, LdapAuthProvider

    for cfg in await get_enabled_providers(db):
        if cfg.provider_type != "ldap":
            continue
        if not LDAP3_AVAILABLE:
            raise HTTPException(status_code=503, detail="ldap3 is not installed on the platform")
        try:
            config = _json.loads(cfg.config_json or "{}")
        except ValueError:
            continue
        return cfg, LdapAuthProvider(config=config)
    return None


@router.post("/ad/test")
async def test_ad_connection(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    """Test the directory connection of the configured LDAP identity provider."""
    from ..config import settings

    found = await _enabled_ldap_provider(db)
    if found is not None:
        cfg, provider = found
        ok, msg = provider.test_connection()
        return {"success": ok, "message": f"{cfg.provider_name}: {msg}", "mode": "ldap"}
    if settings.debug:
        return ad_client.test_connection()
    return {
        "success": False,
        "message": "No LDAP identity provider is enabled. Configure one under "
                   "Admin → Platform → Authentication; this panel tests and "
                   "searches that provider.",
        "mode": "unconfigured",
    }


@router.post("/ad/provision", response_model=UserListResponse, status_code=201)
async def provision_ad_user(
    body: ProvisionADUserRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    """Pre-create an account for a directory user before their first login.

    The row is an LDAP-provider identity (no password); the user signs in with
    their directory credentials and provision_user() matches this row by
    username. The assigned role STICKS across logins as long as the LDAP
    provider has no group→role mapping configured — with a mapping, AD groups
    are the source of truth on every sign-in (see auth/providers/chain.py).
    """
    if body.role not in ("admin", "developer", "user"):
        raise HTTPException(status_code=400, detail="Invalid role")
    username = body.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")
    if await auth_service.get_user_by_username(db, username):
        raise HTTPException(status_code=409, detail="That user already has an account.")

    from ..secrets.models import AuditLog
    new_user = User(
        username=username,
        display_name=(body.display_name or "").strip() or username,
        email=(body.email or "").strip(),
        role=body.role,
        auth_provider="ldap",
        external_id=username,
    )
    db.add(new_user)
    await db.flush()  # id needed for the audit row (defaults land at flush)
    db.add(AuditLog(
        user_id=user.id,
        action="admin.ad_provision",
        resource_type="user",
        resource_id=new_user.id,
        details=f"Pre-provisioned directory user '{username}' as {body.role}",
    ))
    await db.commit()
    return _user_list_response(new_user)


@router.get("/ad/search")
async def search_ad_users(
    q: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    """Search the configured LDAP directory for users."""
    from ..config import settings
    from ..auth.providers.ldap_provider import LdapSearchError

    found = await _enabled_ldap_provider(db)
    if found is not None:
        _cfg, provider = found
        try:
            return provider.search_users(q)
        except LdapSearchError as e:
            raise HTTPException(status_code=400, detail=str(e))
    if settings.debug:
        return ad_client.search_users(q)
    raise HTTPException(
        status_code=400,
        detail="No LDAP identity provider is enabled — configure one under "
               "Admin → Platform → Authentication to search your directory.",
    )
