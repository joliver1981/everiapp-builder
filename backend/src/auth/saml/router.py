"""SAML SP routes: metadata, SP-initiated login, and the ACS callback."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import settings as app_settings
from ...database import get_db
from ..cookies import set_refresh_cookie
from ..models import IdentityProviderConfig, RefreshToken
from ..service import auth_service
from ..providers.chain import provision_user
from . import service as saml_service
from .settings_builder import (
    build_saml_settings,
    extract_identity,
    resolve_role,
    validate_saml_config,
)

logger = logging.getLogger(__name__)
router = APIRouter()


async def _load_provider(db: AsyncSession, provider_id: str) -> IdentityProviderConfig:
    cfg = (await db.execute(
        select(IdentityProviderConfig).where(IdentityProviderConfig.id == provider_id)
    )).scalar_one_or_none()
    if not cfg or cfg.provider_type != "saml":
        raise HTTPException(status_code=404, detail="SAML provider not found")
    if not cfg.is_enabled:
        raise HTTPException(status_code=403, detail="SAML provider is disabled")
    return cfg


def _config_dict(cfg: IdentityProviderConfig) -> dict:
    try:
        return json.loads(cfg.config_json or "{}")
    except json.JSONDecodeError:
        return {}


def _settings_for(request: Request, provider_id: str, config: dict) -> dict:
    base = str(request.base_url)
    return build_saml_settings(config, base, provider_id)


def _post_login_url(config: dict, fragment: str) -> str:
    target = config.get("post_login_redirect") or "/login"
    return f"{target}#{fragment}"


@router.get("/providers")
async def list_saml_providers(db: AsyncSession = Depends(get_db)):
    """Public list of enabled SAML providers, for rendering SSO buttons on the
    login page. Returns only id + display name — no config."""
    rows = (await db.execute(
        select(IdentityProviderConfig).where(
            IdentityProviderConfig.provider_type == "saml",
            IdentityProviderConfig.is_enabled == True,  # noqa: E712
        ).order_by(IdentityProviderConfig.provider_name.asc())
    )).scalars().all()
    return [{"id": p.id, "name": p.provider_name} for p in rows]


@router.get("/{provider_id}/metadata")
async def metadata(provider_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    cfg = await _load_provider(db, provider_id)
    config = _config_dict(cfg)
    try:
        xml, errors = saml_service.sp_metadata(_settings_for(request, provider_id, config))
    except saml_service.SamlNotInstalled as e:
        raise HTTPException(status_code=501, detail=str(e))
    if errors:
        raise HTTPException(status_code=500, detail=f"Invalid SP metadata: {errors}")
    return Response(content=xml, media_type="application/xml")


@router.get("/{provider_id}/login")
async def login(provider_id: str, request: Request, return_to: str | None = None,
                db: AsyncSession = Depends(get_db)):
    cfg = await _load_provider(db, provider_id)
    config = _config_dict(cfg)
    problems = validate_saml_config(config)
    if problems:
        raise HTTPException(status_code=400, detail={"error": "saml_config_invalid", "problems": problems})
    try:
        req_data = await saml_service.prepare_request_data(request)
        url = saml_service.build_login_redirect(
            req_data, _settings_for(request, provider_id, config), return_to,
        )
    except saml_service.SamlNotInstalled as e:
        raise HTTPException(status_code=501, detail=str(e))
    return RedirectResponse(url, status_code=302)


@router.post("/{provider_id}/acs")
async def acs(provider_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    cfg = await _load_provider(db, provider_id)
    config = _config_dict(cfg)
    try:
        group_map = json.loads(cfg.group_role_mapping or "{}")
    except json.JSONDecodeError:
        group_map = {}

    try:
        req_data = await saml_service.prepare_request_data(request)
        errors, attributes, name_id = saml_service.process_acs(
            req_data, _settings_for(request, provider_id, config),
        )
    except saml_service.SamlNotInstalled as e:
        raise HTTPException(status_code=501, detail=str(e))

    if errors:
        logger.warning("SAML ACS validation failed for provider %s: %s", provider_id, errors)
        return RedirectResponse(_post_login_url(config, "saml_error=" + quote("validation_failed")),
                                status_code=302)

    mapping = config.get("attribute_mapping") or {}
    result = extract_identity(attributes, name_id, mapping)
    if not result.success:
        return RedirectResponse(_post_login_url(config, "saml_error=" + quote("no_username")),
                                status_code=302)

    role = resolve_role(result.groups, group_map, cfg.default_role or "user")
    user = await provision_user(
        db, auth_provider="saml", result=result, role=role, auto_provision=cfg.auto_provision,
    )
    if user is None:
        return RedirectResponse(_post_login_url(config, "saml_error=" + quote("not_provisioned")),
                                status_code=302)

    # Issue our own tokens (same shape as a password login).
    access_token = auth_service.create_access_token(user.id, user.role, extra_claims={"username": user.username})
    refresh_value = auth_service.create_refresh_token_value()
    db.add(RefreshToken(
        user_id=user.id,
        token_hash=auth_service.hash_token(refresh_value),
        expires_at=datetime.now(timezone.utc) + timedelta(days=app_settings.jwt_refresh_token_expire_days),
    ))
    await db.commit()

    resp = RedirectResponse(_post_login_url(config, "access_token=" + quote(access_token)),
                            status_code=302)
    set_refresh_cookie(request, resp, refresh_value)
    return resp
