"""App-facing server-function routes.

Mounted at /api/apps so the routes are:
    GET  /api/apps/{app_id}/fn          — list this app's server functions
    POST /api/apps/{app_id}/fn/{name}   — invoke one

Both carry {app_id}, so the scoped-token guard (_enforce_token_scope) confines
a running app's injected preview/embed token to its own functions — the same
free containment the connection-call and app-DB routes rely on.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..apps.service import apps_service
from ..auth.dependencies import SCOPED_TOKEN_PURPOSES, get_current_user, security
from ..auth.models import User
from ..auth.service import auth_service
from ..config import settings
from ..database import get_db
from . import service as fn_service
from .service import FunctionError

router = APIRouter()


class _InvokeIn(BaseModel):
    args: Any = None


def _base_url(request: Request) -> str:
    """Loopback URL the function child dials back on. Uvicorn fills
    scope['server'] from the bound socket; settings.port is the fallback for
    transports that don't (TestClient — harmless there, a function that never
    touches ctx never dials back)."""
    server = request.scope.get("server") or (None, None)
    port = server[1] or settings.port
    return f"http://127.0.0.1:{port}"


def _child_token(request: Request, user: User, app_id: str,
                 credentials: HTTPAuthorizationCredentials | None) -> str:
    """The token the function's ctx calls run under. An already-scoped token
    (preview/embed) is forwarded as-is. A full login session is NEVER handed
    to AI-generated code — mint a fresh app-scoped token carrying the caller's
    identity instead (same deny-by-default containment as the injected
    window.__AIHUB_TOKEN__)."""
    payload = getattr(request.state, "token_payload", None) or {}
    if payload.get("purpose") in SCOPED_TOKEN_PURPOSES and credentials:
        return credentials.credentials
    return auth_service.create_access_token(
        user.id, user.role, expire_minutes=15,
        extra_claims={"purpose": "preview", "app_id": app_id, "username": user.username},
    )


@router.get("/{app_id}/fn")
async def list_functions(
    app_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """This app's server functions (from the tree this caller executes)."""
    app = await apps_service.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="App not found")
    payload = getattr(request.state, "token_payload", None)
    return fn_service.list_functions(app, payload)


@router.post("/{app_id}/fn/{name}")
async def invoke_function(
    app_id: str,
    name: str,
    body: _InvokeIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Run one server function with JSON args; returns its JSON result."""
    # Before name resolution: every attempt consumes a token, so a retry storm
    # against a missing function can't spawn interpreters.
    from ..rate_limit import fn_limiter
    if not fn_limiter.allow(app_id):
        raise HTTPException(status_code=429, detail="Server-function rate limit exceeded; slow down.")
    app = await apps_service.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="App not found")
    payload = getattr(request.state, "token_payload", None)
    try:
        return await fn_service.invoke_function(
            db, app=app, name=name, args=body.args,
            token=_child_token(request, user, app_id, credentials),
            base_url=_base_url(request), user=user, token_payload=payload,
        )
    except FunctionError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except Exception as e:
        # Runner/transport failure — never a 500 with a stack trace.
        raise HTTPException(status_code=502, detail=f"Server function failed: {type(e).__name__}")
