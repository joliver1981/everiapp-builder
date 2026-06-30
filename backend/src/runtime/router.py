"""Runtime router — start/stop apps and proxy all traffic to running Vite dev servers."""
import time

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user
from ..auth.models import User
from ..database import get_db
from ..apps.service import apps_service
from .manager import AppProcess, runtime_manager
from .proxy import proxy_http, proxy_websocket
from .schemas import AppStartRequest, AppStatusResponse


def _proc_to_response(app_id: str, proc: AppProcess | None) -> AppStatusResponse:
    if proc is None:
        return AppStatusResponse(app_id=app_id, status="stopped")
    elapsed = None
    if proc.phase_started_at:
        elapsed = round(time.monotonic() - proc.phase_started_at, 2)
    return AppStatusResponse(
        app_id=app_id,
        status=proc.status,
        # Surface the port even while starting — once the runtime is ready the
        # client can use it without re-polling.
        port=proc.port if proc.port and proc.status in ("running", "starting") else None,
        source=proc.source,
        error=proc.error,
        phase=proc.phase,
        phase_detail=proc.phase_detail,
        phase_elapsed_seconds=elapsed,
    )

# API router — mounted at /api/apps (same prefix as apps CRUD)
api_router = APIRouter()

# Proxy router — mounted at /apps (no /api prefix, serves actual app content)
proxy_router = APIRouter()


# ---- API endpoints (under /api/apps/{app_id}/runtime) ----

@api_router.post("/{app_id}/runtime/start", response_model=AppStatusResponse)
async def start_app(
    app_id: str,
    body: AppStartRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Start (or restart) an app's dev server."""
    app = await apps_service.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="App not found")

    source = body.source if body else "draft"
    proc = await runtime_manager.start_app(app_id, source)
    return _proc_to_response(app_id, proc)


@api_router.post("/{app_id}/runtime/stop")
async def stop_app(
    app_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Stop an app's dev server."""
    app = await apps_service.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="App not found")

    await runtime_manager.stop_app(app_id)
    return {"status": "stopped"}


@api_router.get("/{app_id}/runtime/status", response_model=AppStatusResponse)
async def app_status(
    app_id: str,
    user: User = Depends(get_current_user),
):
    """Get current status of an app's dev server (poll while status='starting')."""
    return _proc_to_response(app_id, runtime_manager.get_status(app_id))


# ---- Catch-all proxy for running apps (under /apps/{app_id}) ----

@proxy_router.websocket("/{app_id}/{path:path}")
async def proxy_app_ws(ws: WebSocket, app_id: str, path: str = ""):
    """WebSocket proxy — used by Vite HMR."""
    proc = runtime_manager.get_status(app_id)
    if not proc or proc.status != "running":
        await ws.close(code=1011, reason="App not running")
        return
    # Vite serves under base=/apps/{id}/, so its HMR websocket lives there too — forward the
    # full base-prefixed path (matches proxy_http). If HMR still can't reconnect, the preview
    # falls back to a full reload, which is harmless.
    await proxy_websocket(ws, proc.port, f"apps/{app_id}/{path}")


@proxy_router.api_route(
    "/{app_id}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def proxy_app_http(request: Request, app_id: str, path: str = ""):
    """HTTP proxy — serves the app UI and all Vite assets."""
    proc = runtime_manager.get_status(app_id)
    if not proc or proc.status != "running":
        raise HTTPException(status_code=502, detail="App is not running. Start it first.")

    # Try to extract user context from Authorization header
    user_info = None
    token = None
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        from ..auth.service import auth_service
        payload = auth_service.decode_access_token(token)
        if payload:
            user_info = {"id": payload["sub"], "username": payload.get("username", ""), "role": payload.get("role", "")}

    # Also check for token in cookie (for iframe scenarios)
    if not token:
        token = request.cookies.get("access_token")
        if token:
            from ..auth.service import auth_service
            payload = auth_service.decode_access_token(token)
            if payload:
                user_info = {"id": payload["sub"], "username": payload.get("username", ""), "role": payload.get("role", "")}

    # Finally, accept a token via the ?__aihub_token= query param. The builder's Preview iframe
    # uses this: an iframe navigation can't set an Authorization header, and AIHub keeps the
    # access token in localStorage (not a cookie), so this is how the dev's token reaches the
    # injected SDK globals (window.__AIHUB_TOKEN__) for dataset/app-DB calls.
    if not token:
        token = request.query_params.get("__aihub_token")
        if token:
            from ..auth.service import auth_service
            payload = auth_service.decode_access_token(token)
            if payload:
                user_info = {"id": payload["sub"], "username": payload.get("username", ""), "role": payload.get("role", "")}

    return await proxy_http(request, proc.port, app_id, path, user=user_info, token=token)


@proxy_router.api_route("/{app_id}", methods=["GET"])
async def proxy_app_root(request: Request, app_id: str):
    """Handle bare /apps/{app_id} — redirect to /apps/{app_id}/."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/apps/{app_id}/")
