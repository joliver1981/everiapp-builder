"""App-facing connection routes: attach a connection to an app (builder) and
make free-form calls through it (the running app's callConnection).

Split out from the admin `connections/router.py` because these are reached with
the app's own scoped token — they carry `{app_id}` so the scoped-token guard
confines them to that app, exactly like the datasets bindings/runtime routes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user, require_role
from ..auth.models import User
from ..database import get_db
from . import app_calls
from . import providers as ai_providers
from .app_calls import ExternalCallError
from .service import connections_service

bindings_router = APIRouter()      # mounted at /api/apps
discoverable_router = APIRouter()  # mounted at /api/connections


def _pub(conn) -> dict:
    """Non-secret identity for the builder/app: base_url is fine to show, config
    (which may hold auth params) is not. AI connections additionally expose
    their provider + curated model list — that's what makes them first-class
    for apps (model pickers, default model) without leaking any credential."""
    cfg = conn.config or {}
    out = {
        "id": conn.id,
        "name": conn.name,
        "description": conn.description or "",
        "kind": conn.kind,
        "base_url": cfg.get("base_url", ""),
        "app_callable": conn.app_callable,
    }
    if conn.kind == "ai":
        out.update({
            "provider": cfg.get("provider") or "custom",
            "api_format": ai_providers.ai_api_format(cfg),
            "models": ai_providers.ai_models(cfg),
            "default_model": cfg.get("default_model") or None,
            "chat_path": ai_providers.ai_chat_path(cfg),
        })
    return out


class _CallIn(BaseModel):
    method: str = "GET"
    path: str
    query: dict[str, Any] | None = None
    headers: dict[str, Any] | None = None
    body: Any | None = None


@bindings_router.get("/{app_id}/connections")
async def list_app_connections(
    app_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Connections currently bound to this app (what callConnection can reach)."""
    return [_pub(c) for c in await app_calls.list_bound_connections(db, app_id)]


@bindings_router.post("/{app_id}/connections/{connection_id}", status_code=status.HTTP_201_CREATED)
async def bind_app_connection(
    app_id: str,
    connection_id: str,
    db: AsyncSession = Depends(get_db),
    # Builder/admin action — require a real login session. require_role rejects
    # preview/embed scoped tokens, so a running app's own JS can't self-attach a
    # connection to escalate its reach.
    user: User = Depends(require_role("admin", "developer")),
):
    """Idempotent: 201 whether newly created or already present."""
    try:
        await app_calls.bind_connection(db, app_id, connection_id, user.id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    conn = await connections_service.get_connection(db, connection_id)
    return _pub(conn) if conn else {"id": connection_id}


@bindings_router.delete("/{app_id}/connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unbind_app_connection(
    app_id: str,
    connection_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    removed = await app_calls.unbind_connection(db, app_id, connection_id, user.id)
    if not removed:
        raise HTTPException(status_code=404, detail="Binding not found")


@bindings_router.post("/{app_id}/connections/{connection_id}/call")
async def call_connection(
    app_id: str,
    connection_id: str,
    body: _CallIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Make one outbound HTTP call through a bound, app-callable connection."""
    from ..rate_limit import external_call_limiter
    if not external_call_limiter.allow(app_id):
        raise HTTPException(status_code=429, detail="External-call rate limit exceeded; slow down.")
    try:
        return await app_calls.execute_app_call(
            db, app_id=app_id, connection_id=connection_id, method=body.method,
            path=body.path, query=body.query, headers=body.headers, body=body.body,
            user_id=user.id,
        )
    except ExternalCallError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except Exception as e:
        # Transport/upstream failure — never a 500 with a stack trace.
        raise HTTPException(status_code=502, detail=f"External call failed: {type(e).__name__}")


@discoverable_router.get("/callable")
async def list_callable_connections(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    """REST + AI-provider connections an admin has marked app-callable — the
    pick-list the builder shows for attaching a connection to an app. Uses the
    same non-secret serialization as the bound list (config is scrubbed
    upstream by list_connections, and _pub only whitelists safe fields)."""
    conns = await connections_service.list_connections(db)
    return [
        _pub(c) for c in conns
        if c.kind in app_calls.APP_CALLABLE_KINDS and c.app_callable
    ]
