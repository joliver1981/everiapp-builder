"""Authentication provider chain.

Tries each enabled external provider (LDAP today) in order, then falls back to
the existing mock/local auth. Adapted from the production-tested
provider_chain.py but converted to AIHub's async SQLAlchemy + string roles.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import IdentityProviderConfig, User
from .base import AuthResult

logger = logging.getLogger(__name__)


async def get_enabled_providers(db: AsyncSession) -> list[IdentityProviderConfig]:
    """Enabled providers, default-first then by created order."""
    result = await db.execute(
        select(IdentityProviderConfig)
        .where(IdentityProviderConfig.is_enabled == True)  # noqa: E712
        .order_by(IdentityProviderConfig.is_default.desc(), IdentityProviderConfig.created_at.asc())
    )
    return list(result.scalars().all())


async def authenticate_external(db: AsyncSession, username: str, password: str) -> User | None:
    """Try each external provider. On success, provision/update the local User
    and return it. Returns None if no external provider authenticated (caller
    then falls back to mock/local).
    """
    providers = await get_enabled_providers(db)
    for cfg in providers:
        if cfg.provider_type == "ldap":
            user = await _try_ldap(db, username, password, cfg)
            if user is not None:
                return user
    return None


async def _try_ldap(db: AsyncSession, username: str, password: str,
                    cfg: IdentityProviderConfig) -> User | None:
    try:
        from .ldap_provider import LDAP3_AVAILABLE, LdapAuthProvider
        if not LDAP3_AVAILABLE:
            logger.warning("ldap3 not installed; skipping LDAP provider %s", cfg.provider_name)
            return None

        try:
            config = json.loads(cfg.config_json or "{}")
        except json.JSONDecodeError:
            logger.error("Invalid config_json for provider %s", cfg.id)
            return None
        try:
            group_map = json.loads(cfg.group_role_mapping or "{}")
        except json.JSONDecodeError:
            group_map = {}

        provider = LdapAuthProvider(config=config, group_role_mapping=group_map,
                                    default_role=cfg.default_role or "user")
        result = provider.authenticate(username, password)
        if not result.success:
            # Auth failed at this provider — return None so the chain can fall
            # through to mock/local. (Bad LDAP creds shouldn't block a local
            # admin from logging in during initial setup.)
            return None

        role = provider.resolve_role(result.groups)
        return await provision_user(
            db, auth_provider="ldap", result=result, role=role,
            auto_provision=cfg.auto_provision,
        )
    except Exception as e:
        logger.error("LDAP auth error for provider %s: %s", cfg.provider_name, e)
        return None


async def provision_user(db: AsyncSession, *, auth_provider: str, result: AuthResult,
                         role: str, auto_provision: bool) -> User | None:
    """Find-or-create a local User from an external AuthResult.

    Matches on (auth_provider, external_id) first, then username. Updates the
    record on repeat logins so display name / email / groups / role stay fresh.
    """
    # Try by external identity
    existing = (await db.execute(
        select(User).where(
            User.auth_provider == auth_provider,
            User.external_id == result.external_id,
        )
    )).scalar_one_or_none()

    # Fall back to username match (e.g. a local 'admin' that now uses LDAP)
    if existing is None:
        existing = (await db.execute(
            select(User).where(User.username == result.username)
        )).scalar_one_or_none()

    if existing is None and not auto_provision:
        logger.info("LDAP user %s not provisioned (auto_provision disabled)", result.username)
        return None

    if existing is None:
        user = User(
            username=result.username,
            display_name=result.display_name or result.username,
            email=result.email or "",
            role=role,
            ad_groups=json.dumps(result.groups),
            auth_provider=auth_provider,
            external_id=result.external_id,
        )
        db.add(user)
        await db.flush()
        return user

    # Update existing
    existing.display_name = result.display_name or existing.display_name
    existing.email = result.email or existing.email
    existing.role = role
    existing.ad_groups = json.dumps(result.groups)
    existing.auth_provider = auth_provider
    existing.external_id = result.external_id
    existing.updated_at = datetime.now(timezone.utc)
    return existing
