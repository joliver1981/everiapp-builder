"""Embedding endpoints: config (admin/dev), token mint, public framed bootstrap."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..apps.models import App
from ..auth.dependencies import require_role
from ..auth.models import User
from ..database import get_db
from ..secrets.models import AuditLog
from . import service

router = APIRouter()


class EmbedConfigIn(BaseModel):
    enabled: bool
    allowed_origins: list[str] = []


async def _get_app(db: AsyncSession, app_id: str) -> App:
    app = (await db.execute(select(App).where(App.id == app_id))).scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="App not found")
    return app


def _embed_url(request: Request, app_id: str) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/api/apps/{app_id}/embed"


@router.get("/{app_id}/embed-config")
async def get_embed_config(
    app_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("admin", "developer")),
):
    app = await _get_app(db, app_id)
    origins = service.parse_origins(app.embed_allowed_origins)
    url = _embed_url(request, app_id)
    return {
        "enabled": app.embed_enabled,
        "allowed_origins": origins,
        "embed_url": url,
        "snippet": service.iframe_snippet(url) if app.embed_enabled else "",
    }


@router.put("/{app_id}/embed-config")
async def set_embed_config(
    app_id: str,
    body: EmbedConfigIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    app = await _get_app(db, app_id)
    try:
        origins = service.validate_origins(body.allowed_origins)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    app.embed_enabled = body.enabled
    app.embed_allowed_origins = ",".join(origins)
    db.add(AuditLog(
        user_id=user.id, action="app.embed.config",
        resource_type="app", resource_id=app_id,
        details=f"embed_enabled={body.enabled} origins={origins or ['*']}",
    ))
    await db.commit()

    url = _embed_url(request, app_id)
    return {
        "enabled": app.embed_enabled,
        "allowed_origins": origins,
        "embed_url": url,
        "snippet": service.iframe_snippet(url) if app.embed_enabled else "",
    }


@router.post("/{app_id}/embed-token")
async def create_embed_token(
    app_id: str,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("admin", "developer")),
):
    """Mint an embed credential for integrators who frame the app URL
    directly instead of using the /embed bootstrap (which mints its own).
    Append it as ?__aihub_embed=<token> on /apps/{app_id}/view — the runtime
    proxy verifies it and injects an app-scoped guest session token."""
    app = await _get_app(db, app_id)
    if not app.embed_enabled:
        raise HTTPException(status_code=409, detail="Embedding is not enabled for this app")
    token, ttl = service.mint_embed_token(app_id)
    return {
        "token": token,
        "expires_in": ttl,
        "usage": f"/apps/{app_id}/view?__aihub_embed=<token>",
    }


@router.get("/{app_id}/embed", response_class=HTMLResponse)
async def embed_bootstrap(
    app_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Public framed bootstrap. Returns a full-bleed page that hosts the app and
    declares which parents may frame it (CSP frame-ancestors). 404 unless the
    app has embedding enabled."""
    app = await _get_app(db, app_id)
    if not app.embed_enabled:
        raise HTTPException(status_code=404, detail="Embedding not enabled")

    origins = service.parse_origins(app.embed_allowed_origins)
    csp = f"frame-ancestors {service.frame_ancestors(origins)}"
    base = str(request.base_url).rstrip("/")
    # Mint the embed credential HERE (this endpoint already gates on
    # embed_enabled) and hand it to the inner iframe: the runtime proxy
    # verifies it and injects an app-scoped GUEST session token. Without it,
    # anonymous embedded viewers had no token transport at all and every SDK
    # call in the embedded app 401'd.
    embed_token, _ttl = service.mint_embed_token(app_id)
    inner = f"{base}/apps/{app_id}/view?__aihub_embed={embed_token}"
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{app.name}</title>"
        "<style>html,body{margin:0;height:100%}iframe{border:0;width:100%;height:100%}</style>"
        "</head><body>"
        f"<iframe src='{inner}' allow='clipboard-read; clipboard-write'></iframe>"
        "</body></html>"
    )
    # Explicitly drop X-Frame-Options (CSP frame-ancestors supersedes it) and set
    # the allow-list. Same-origin parents always work via 'self'.
    return HTMLResponse(content=html, headers={"Content-Security-Policy": csp})
