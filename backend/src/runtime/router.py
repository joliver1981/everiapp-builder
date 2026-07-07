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
from .proxy import proxy_http, proxy_websocket, retry_page
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

# Lifetime of the token minted into a served preview/viewer page
# (window.__AIHUB_TOKEN__). Long enough for a full working session in the
# Preview iframe — the app has no way to refresh it once booted.
PREVIEW_TOKEN_TTL_MINUTES = 12 * 60

# The synthetic platform user embedded (anonymous) viewers act as. SDK
# endpoints resolve the token's sub against the users table and scope app-db
# writes by it, so embed sessions need a REAL row — one shared guest with the
# weakest role. Created lazily on first embedded view.
EMBED_GUEST_USERNAME = "embed-guest"


async def _ensure_embed_guest():
    """Return the shared embed-guest User, creating it on first use."""
    from sqlalchemy import select

    from ..auth.models import User
    from ..database import async_session

    async with async_session() as db:
        user = (await db.execute(
            select(User).where(User.username == EMBED_GUEST_USERNAME)
        )).scalar_one_or_none()
        # Callers read guest.id/.username/.role AFTER this session closes — safe
        # only because the sessionmaker sets expire_on_commit=False (see
        # database.py); the columns are populated at flush, so no lazy refresh
        # fires post-close. If that setting ever flips, read the attrs before
        # the `async with` exits.
        if user:
            return user
        user = User(
            username=EMBED_GUEST_USERNAME,
            display_name="Embedded viewer",
            role="user",
        )
        db.add(user)
        try:
            await db.commit()
        except Exception:
            # Lost a create race to a concurrent embedded view — use theirs.
            await db.rollback()
            user = (await db.execute(
                select(User).where(User.username == EMBED_GUEST_USERNAME)
            )).scalar_one_or_none()
        return user

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

        # Re-vendor the platform SDK into the draft on every preview start —
        # existing apps keep the SDK snapshotted at generation time, so fixes
        # (session-expiry handling, deployed-app URL bugs) never reached them.
        # Best-effort and byte-compare-gated (see sync_vendored_sdk): identical
        # files are never rewritten, so a running Vite doesn't HMR-reload.
        # Draft only — version snapshots are immutable by design.
        try:
            from pathlib import Path

            from ..config import settings
            from ..apps.service import sync_vendored_sdk

            frontend_dir = Path(settings.app_data_dir).resolve() / app_id / "draft" / "frontend"
            updated = sync_vendored_sdk(frontend_dir)
            if updated:
                logger.info("re-vendored SDK for %s: %s", app_id, ", ".join(updated))
        except Exception:
            logger.exception("SDK re-vendor on start failed for %s (non-fatal)", app_id)

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
        # ACCEPT the handshake, then close cleanly — never reject it. Vite's
        # "polling for restart" pings ARE WebSocket connects (subprotocol
        # 'vite-ping') that count as success only when the socket OPENS; a
        # pre-accept close() goes out as an HTTP 403 rejection, which the
        # client reads as "still down" and retries every second, forever —
        # one idle preview tab flooded the log with ~2k `connection rejected`
        # lines after a backend restart emptied the runtime manager. Opening
        # the socket lets the ping "succeed", so the page reloads once, lands
        # on retry_page (bounded, self-healing HTTP polling), and the storm
        # never starts.
        requested = list(ws.scope.get("subprotocols") or [])
        try:
            # Echo a requested subprotocol or the browser fails the handshake
            # before 'open' ever fires (per spec) — back to the reject storm.
            await ws.accept(subprotocol=requested[0] if requested else None)
            await ws.close(code=1012, reason="App not running")
        except Exception:
            pass
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
        # Must be a framable HTML page, NOT an HTTPException: a JSON 502 gets
        # X-Frame-Options from SecurityHeadersMiddleware and the builder's
        # cross-origin Preview iframe renders it as a silent blank screen.
        # The page self-retries, so an iframe mounted a beat too early (or a
        # runtime that is still starting) heals without a manual reload.
        if proc and proc.status == "starting":
            reason = "The app is still starting up — hang tight."
        elif proc and proc.status == "error":
            reason = f"The app failed to start: {(proc.error or 'unknown error')[:300]}"
        else:
            reason = "The app is not running. Start it from the builder's Preview tab."
        return retry_page(reason)

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
    transports = ("authorization header", "access_token cookie", "__aihub_token query")
    candidates = [
        auth_header[7:] if auth_header.startswith("Bearer ") else None,
        request.cookies.get("access_token"),
        request.query_params.get("__aihub_token"),
    ]
    won = None
    for name, candidate in zip(transports, candidates):
        if not candidate:
            continue
        payload = auth_service.decode_access_token(candidate)
        if payload:
            won = name
            user_info = {"id": payload["sub"], "username": payload.get("username", ""), "role": payload.get("role", "")}
            # The winning credential proved WHO this is, but it may be minutes
            # from expiry (operators run short access TTLs — 15 min here).
            # The app keeps the injected __AIHUB_TOKEN__ for its whole session
            # with no refresh path, so forwarding the incoming token means SDK
            # calls start 401ing mid-session ("app-db migrate failed (401)")
            # in any preview left open past the TTL. Mint a fresh
            # preview-session token instead — same user, same role, but
            # SCOPED: purpose=preview + app_id make it rejectable by
            # require_role (admin/builder surfaces) and unusable against any
            # other app — the app's own JS can read this token, so it must
            # never be a general-purpose credential.
            token = auth_service.create_access_token(
                user_info["id"], user_info["role"],
                expire_minutes=PREVIEW_TOKEN_TTL_MINUTES,
                extra_claims={
                    "purpose": "preview",
                    "app_id": app_id,
                    "username": user_info["username"],
                },
            )
            break

    # Embedded (anonymous) viewers: the public embed bootstrap appends a
    # signed, app-bound embed token to the inner iframe URL. Verify it and
    # mint an app-scoped GUEST session token — without this, embedded
    # data-backed apps rendered their shell and then 401'd on every SDK call.
    if not token:
        embed_candidate = request.query_params.get("__aihub_embed")
        if embed_candidate:
            from ..embedding.service import verify_embed_token

            embed_app_id = verify_embed_token(embed_candidate)
            if embed_app_id == app_id:
                guest = await _ensure_embed_guest()
                if guest:
                    won = "__aihub_embed query"
                    user_info = {"id": guest.id, "username": guest.username, "role": guest.role}
                    token = auth_service.create_access_token(
                        guest.id, guest.role,
                        expire_minutes=PREVIEW_TOKEN_TTL_MINUTES,
                        extra_claims={
                            "purpose": "embed",
                            "app_id": app_id,
                            "username": guest.username,
                        },
                    )

    # One line per app DOCUMENT load (not per asset): when an app's SDK calls
    # all 401 ("Invalid or expired token"), this says whether the page got a
    # token injected and from which transport — or that every supplied
    # credential was stale/absent.
    if not path:
        if won:
            logger.info("preview %s: injecting token from %s", app_id, won)
        else:
            supplied = [n for n, c in zip(transports, candidates) if c]
            if request.query_params.get("__aihub_embed"):
                supplied.append("__aihub_embed query")
            logger.warning(
                "preview %s: NO valid token to inject (supplied: %s) — the app's SDK calls will 401",
                app_id, ", ".join(supplied) or "none",
            )

    return await proxy_http(request, proc.port, app_id, path, user=user_info, token=token)


@proxy_router.api_route("/{app_id}", methods=["GET"])
async def proxy_app_root(request: Request, app_id: str):
    """Handle bare /apps/{app_id} — redirect to /apps/{app_id}/."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/apps/{app_id}/")
