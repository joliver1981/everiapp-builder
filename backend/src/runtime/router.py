"""Runtime router — start/stop apps and proxy all traffic to running Vite dev servers."""
import logging
import time

logger = logging.getLogger(__name__)

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

    # Self-heal the decision registry from the draft manifest on every preview
    # start — best-effort; a bad manifest must never block the preview. (The
    # generation hook only fires on turns that re-emit decisions.json, so a
    # manifest written under an older backend process would otherwise drift
    # and every aiDecide would 404.)
    if source == "draft":
        try:
            from ..decisions.service import sync_from_draft
            _written, sync_errors = await sync_from_draft(db, app_id)
            if sync_errors:
                logger.warning("decision sync for %s rejected entries: %s",
                               app_id, "; ".join(sync_errors))
        except Exception:
            logger.exception("decision sync on start failed for %s (non-fatal)", app_id)

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

    # Extract user context. Three transports, first VALID one wins — an expired
    # or garbage token is discarded (never injected into the page) so the next
    # transport can still authenticate the request:
    #   1. Authorization header (programmatic calls);
    #   2. `access_token` cookie scoped to /apps — the app viewer sets this
    #      (an iframe navigation can't send a header, and a query param would
    #      leak the token into the address bar, history, and access logs);
    #   3. ?__aihub_token= query param — the builder's Preview iframe.
    from ..auth.service import auth_service

    user_info = None
    token = None
    auth_header = request.headers.get("authorization", "")
    candidates = [
        auth_header[7:] if auth_header.startswith("Bearer ") else None,
        request.cookies.get("access_token"),
        request.query_params.get("__aihub_token"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        payload = auth_service.decode_access_token(candidate)
        if payload:
            token = candidate
            user_info = {"id": payload["sub"], "username": payload.get("username", ""), "role": payload.get("role", "")}
            break

    return await proxy_http(request, proc.port, app_id, path, user=user_info, token=token)


@proxy_router.api_route("/{app_id}", methods=["GET"])
async def proxy_app_root(request: Request, app_id: str):
    """Handle bare /apps/{app_id} — redirect to /apps/{app_id}/."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/apps/{app_id}/")
