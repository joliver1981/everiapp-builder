"""OIDC SP routes: login redirect + authorization-code callback."""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import settings as app_settings
from ...database import get_db
from ..cookies import cookie_secure, set_refresh_cookie
from ..models import IdentityProviderConfig, RefreshToken
from ..service import auth_service
from ..providers.chain import provision_user
from ..providers.roles import resolve_role
from . import client as oidc_client
from . import service as oidc_service

logger = logging.getLogger(__name__)
router = APIRouter()

_STATE_COOKIE = "oidc_state"


async def _load_provider(db: AsyncSession, provider_id: str) -> IdentityProviderConfig:
    cfg = (await db.execute(
        select(IdentityProviderConfig).where(IdentityProviderConfig.id == provider_id)
    )).scalar_one_or_none()
    if not cfg or cfg.provider_type != "oidc":
        raise HTTPException(status_code=404, detail="OIDC provider not found")
    if not cfg.is_enabled:
        raise HTTPException(status_code=403, detail="OIDC provider is disabled")
    return cfg


def _config(cfg: IdentityProviderConfig) -> dict:
    try:
        return json.loads(cfg.config_json or "{}")
    except json.JSONDecodeError:
        return {}


def _redirect_uri(request: Request, config: dict, provider_id: str) -> str:
    if config.get("redirect_uri"):
        return config["redirect_uri"]
    base = str(request.base_url).rstrip("/")
    return f"{base}/api/auth/oidc/{provider_id}/callback"


def _post_login_url(config: dict, fragment: str) -> str:
    target = config.get("post_login_redirect") or "/login"
    return f"{target}#{fragment}"


@router.get("/providers")
async def list_oidc_providers(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(IdentityProviderConfig).where(
            IdentityProviderConfig.provider_type == "oidc",
            IdentityProviderConfig.is_enabled == True,  # noqa: E712
        ).order_by(IdentityProviderConfig.provider_name.asc())
    )).scalars().all()
    return [{"id": p.id, "name": p.provider_name} for p in rows]


@router.get("/{provider_id}/login")
async def login(provider_id: str, request: Request, return_to: str | None = None,
                db: AsyncSession = Depends(get_db)):
    cfg = await _load_provider(db, provider_id)
    config = _config(cfg)
    problems = oidc_client.validate_oidc_config(config)
    if problems:
        raise HTTPException(status_code=400, detail={"error": "oidc_config_invalid", "problems": problems})

    try:
        disco = await oidc_service.fetch_discovery(config["discovery_url"])
    except oidc_service.OidcError as e:
        raise HTTPException(status_code=502, detail=f"OIDC discovery failed: {e}")

    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    verifier, challenge = oidc_client.pkce_pair()
    redirect_uri = _redirect_uri(request, config, provider_id)

    url = oidc_client.build_authorize_url(
        disco["authorization_endpoint"],
        client_id=config["client_id"], redirect_uri=redirect_uri,
        scopes=config.get("scopes", "openid email profile"),
        state=state, nonce=nonce, code_challenge=challenge,
    )
    state_token = oidc_client.encode_state(
        app_settings.jwt_secret_key, provider_id=provider_id, nonce=nonce,
        code_verifier=verifier, return_to=return_to, state=state,
    )
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(_STATE_COOKIE, state_token, httponly=True, samesite="lax",
                    secure=cookie_secure(request),
                    max_age=oidc_client.STATE_TTL_SECONDS, path="/api/auth/oidc")
    return resp


@router.get("/{provider_id}/callback")
async def callback(provider_id: str, request: Request, db: AsyncSession = Depends(get_db),
                   code: str | None = None, state: str | None = None,
                   error: str | None = None):
    cfg = await _load_provider(db, provider_id)
    config = _config(cfg)

    def _fail(reason: str) -> RedirectResponse:
        r = RedirectResponse(_post_login_url(config, "oidc_error=" + quote(reason)), status_code=302)
        r.delete_cookie(_STATE_COOKIE, path="/api/auth/oidc")
        return r

    if error:
        return _fail(error)
    if not code or not state:
        return _fail("missing_code")

    state_cookie = request.cookies.get(_STATE_COOKIE)
    state_data = oidc_client.decode_state(app_settings.jwt_secret_key, state_cookie or "")
    if not state_data or state_data.get("pid") != provider_id or state_data.get("st") != state:
        return _fail("state_mismatch")

    try:
        disco = await oidc_service.fetch_discovery(config["discovery_url"])
        tokens = await oidc_service.exchange_code(
            disco["token_endpoint"], client_id=config["client_id"],
            client_secret=config.get("client_secret", ""), code=code,
            redirect_uri=_redirect_uri(request, config, provider_id),
            code_verifier=state_data["cv"],
        )
        id_token = tokens.get("id_token")
        if not id_token:
            return _fail("no_id_token")
        claims = await oidc_service.validate_id_token(
            id_token, disco["jwks_uri"], client_id=config["client_id"],
            issuer=disco["issuer"], nonce=state_data.get("nonce"),
        )
        # Enrich with userinfo when available (groups often live there).
        if disco.get("userinfo_endpoint") and tokens.get("access_token"):
            claims = {**(await oidc_service.fetch_userinfo(
                disco["userinfo_endpoint"], tokens["access_token"])), **claims}
    except oidc_service.OidcError as e:
        logger.warning("OIDC callback failed for provider %s: %s", provider_id, e)
        return _fail("exchange_failed")

    result = oidc_client.extract_identity(claims, config.get("attribute_mapping"))
    if not result.success:
        return _fail("no_username")

    try:
        group_map = json.loads(cfg.group_role_mapping or "{}")
    except json.JSONDecodeError:
        group_map = {}
    role = resolve_role(result.groups, group_map, cfg.default_role or "user")

    user = await provision_user(
        db, auth_provider="oidc", result=result, role=role, auto_provision=cfg.auto_provision)
    if user is None:
        return _fail("not_provisioned")

    access_token = auth_service.create_access_token(user.id, user.role)
    refresh_value = auth_service.create_refresh_token_value()
    db.add(RefreshToken(
        user_id=user.id, token_hash=auth_service.hash_token(refresh_value),
        expires_at=datetime.now(timezone.utc) + timedelta(days=app_settings.jwt_refresh_token_expire_days),
    ))
    await db.commit()

    resp = RedirectResponse(_post_login_url(config, "access_token=" + quote(access_token)),
                            status_code=302)
    resp.delete_cookie(_STATE_COOKIE, path="/api/auth/oidc")
    set_refresh_cookie(request, resp, refresh_value)
    return resp
