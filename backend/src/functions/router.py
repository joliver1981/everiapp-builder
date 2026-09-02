"""Server-function routes.

Mounted at /api/apps so the routes are:

  App-facing (the SDK's callFunction; scoped preview/embed tokens allowed):
    GET  /api/apps/{app_id}/fn          — list this app's server functions
    POST /api/apps/{app_id}/fn/{name}   — invoke one

  Developer-facing (the builder's Functions panel; full login, admin/developer):
    GET  /api/apps/{app_id}/functions                — list with summary/callers/etc.
    POST /api/apps/{app_id}/functions                — scaffold a new function file
    POST /api/apps/{app_id}/functions/{name}/run     — test-run against the DRAFT
    GET  /api/apps/{app_id}/functions/calls          — recent calls, all functions
    GET  /api/apps/{app_id}/functions/{name}/calls   — recent calls, one function

Every route carries {app_id}, so the scoped-token guard (_enforce_token_scope)
confines a running app's injected preview/embed token to its own functions —
the same free containment the connection-call and app-DB routes rely on.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from .. import python_env
from ..apps.service import apps_service
from ..auth.dependencies import SCOPED_TOKEN_PURPOSES, get_current_user, require_role, security
from ..auth.models import User
from ..auth.service import auth_service
from ..config import settings
from ..database import get_db
from . import service as fn_service
from .service import FunctionError

router = APIRouter()


class _InvokeIn(BaseModel):
    args: Any = None


class _CreateIn(BaseModel):
    name: str


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


# ---------------------------------------------------------------------------
# Developer-facing: the builder's Functions panel
# ---------------------------------------------------------------------------

async def _dev_app(db: AsyncSession, app_id: str):
    app = await apps_service.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="App not found")
    return app


@router.get("/{app_id}/functions")
async def describe_functions(
    app_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    """The draft's server functions with timeout, summary, size, modified time,
    UI call sites, and published-version presence — plus whether the platform
    can run them at all (no Python runtime → the panel says so upfront)."""
    app = await _dev_app(db, app_id)
    return {
        "functions": fn_service.describe_functions(app),
        "runtime_available": bool(python_env.python_cmd()),
        "published_version": int(app.current_version or 0),
    }


@router.post("/{app_id}/functions", status_code=201)
async def create_function(
    app_id: str,
    body: _CreateIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    """Scaffold server/functions/<name>.py with the handler(args, ctx) contract."""
    app = await _dev_app(db, app_id)
    try:
        path = fn_service.scaffold_function(app, body.name.strip())
    except FunctionError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    return {"name": body.name.strip(), "path": path}


# NOTE: declared before /{name}/calls so "calls" isn't captured as a function name.
@router.get("/{app_id}/functions/calls")
async def list_all_calls(
    app_id: str,
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    await _dev_app(db, app_id)
    return {"calls": await fn_service.list_calls(db, app_id, None, limit)}


@router.get("/{app_id}/functions/{name}/calls")
async def list_function_calls(
    app_id: str,
    name: str,
    limit: int = Query(20, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    await _dev_app(db, app_id)
    return {"calls": await fn_service.list_calls(db, app_id, name, limit)}


@router.post("/{app_id}/functions/{name}/run")
async def run_function(
    app_id: str,
    name: str,
    body: _InvokeIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    """Test-run a function against the DRAFT with the caller's identity.

    A test run's failure is a *result* the developer wants to read (message +
    logs), so function-level errors come back as 200 {ok: false, ...}; only a
    missing function / app is an HTTP error. The run is recorded in the call
    log with trigger "panel".
    """
    from ..rate_limit import fn_limiter
    if not fn_limiter.allow(app_id):
        raise HTTPException(status_code=429, detail="Server-function rate limit exceeded; slow down.")
    app = await _dev_app(db, app_id)
    # Always a preview-scoped token: the panel iterates on the draft, and a full
    # login session is never handed to AI-generated code.
    token = auth_service.create_access_token(
        user.id, user.role, expire_minutes=15,
        extra_claims={"purpose": "preview", "app_id": app_id, "username": user.username},
    )
    try:
        result = await fn_service.invoke_function(
            db, app=app, name=name, args=body.args, token=token,
            base_url=_base_url(request), user=user,
            token_payload={"purpose": "preview", "app_id": app_id}, trigger="panel",
        )
    except FunctionError as e:
        if e.status_code == 404:
            raise HTTPException(status_code=404, detail=str(e))
        return {"ok": False, "error": str(e), "status_code": e.status_code,
                "logs": e.logs, "result": None}
    except Exception as e:
        return {"ok": False, "error": f"Server function failed: {type(e).__name__}",
                "status_code": 502, "logs": [], "result": None}
    return result
