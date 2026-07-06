from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from .service import auth_service
from .models import User

security = HTTPBearer(auto_error=False)

# Session tokens minted for injection into a RUNNING app (window.__AIHUB_TOKEN__:
# the runtime proxy's 12h preview token, and the embed guest token). They carry
# purpose + app_id claims and are deliberately weaker than a login token:
#   - require_role (admin/developer surfaces) rejects them outright — an app's
#     JS can read its injected token, and an admin previewing a generated app
#     must not hand that app admin API access;
#   - app-scoped: they only work on routes for the app they were minted for.
SCOPED_TOKEN_PURPOSES = ("preview", "embed")

# The ONLY non-app-scoped routes a purpose-scoped token (injected into a
# running app as window.__AIHUB_TOKEN__) may reach. Every other thing the SDK
# calls carries {app_id} in the path; this covers fetchUser(). Deny-by-default
# on everything else so an anonymous embed-guest token — or a previewed app's
# admin-identity token — can't read global lists (/api/apps, /api/ai/providers,
# /api/datasets/discoverable, …) that were 401 for an embedded viewer before.
_SCOPED_TOKEN_PATH_ALLOWLIST = frozenset({"/api/auth/me"})


def _enforce_token_scope(payload: dict, request: Request) -> None:
    """Confine app-scoped session tokens to their own app.

    A token minted for injection into a running app (purpose=preview/embed) is
    readable by that app's (AI-generated, possibly hostile) JS, so it must be
    a narrow, app-bound credential — not a general session:
      - on a route WITH an {app_id} param, the token's app_id must match;
      - on a route WITHOUT one, deny unless explicitly allowlisted.
    Privileged surfaces are additionally rejected by require_role.
    """
    if payload.get("purpose") not in SCOPED_TOKEN_PURPOSES:
        return
    route_app_id = request.path_params.get("app_id")
    if route_app_id:
        token_app_id = payload.get("app_id")
        if token_app_id and route_app_id != token_app_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This session token is scoped to a different app",
            )
        return
    if request.url.path in _SCOPED_TOKEN_PATH_ALLOWLIST:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="This session token can only access its own app's endpoints",
    )


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    payload = auth_service.decode_access_token(credentials.credentials)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    _enforce_token_scope(payload, request)
    # Expose the claims to downstream dependencies (require_role checks the
    # token's purpose — the User row alone can't distinguish a login session
    # from an injected preview/embed session of the same user).
    request.state.token_payload = payload

    user = await auth_service.get_user_by_id(db, payload["sub"])
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")

    return user


async def get_current_user_flexible(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Bearer auth for endpoints called from inside apps (the runtime proxy
    injects window.__AIHUB_TOKEN__ and the SDK sends it as a Bearer header).

    Historical note: this used to fall back to the `access_token` cookie, but
    that cookie is scoped to path=/apps while every endpoint using this
    dependency lives under /api — the fallback was dead code and implied a
    transport that never worked. Bearer is the only live transport.
    """
    token = credentials.credentials if credentials else None
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    payload = auth_service.decode_access_token(token)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    _enforce_token_scope(payload, request)
    request.state.token_payload = payload

    user = await auth_service.get_user_by_id(db, payload["sub"])
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")

    return user


def require_role_allow_scoped(*roles: str):
    """Role gate for SDK-FACING endpoints that apps call about THEMSELVES
    (e.g. useAppSchema running /db/migrate on the app's own database): the
    underlying identity's role must match, but app-scoped session tokens are
    allowed through — their app_id scoping (enforced in get_current_user) is
    the containment, and such routes always carry {app_id} in the path. Use
    plain require_role for admin/builder surfaces."""
    async def role_checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return user
    return role_checker


def require_role(*roles: str):
    async def role_checker(request: Request, user: User = Depends(get_current_user)) -> User:
        payload = getattr(request.state, "token_payload", None) or {}
        if payload.get("purpose") in SCOPED_TOKEN_PURPOSES:
            # A preview/embed session token is readable by the app's own JS —
            # it must never unlock role-gated (admin/builder) surfaces, no
            # matter whose identity it carries.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Preview/embed session tokens cannot access this endpoint",
            )
        if user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return user
    return role_checker
