"""Admin endpoints to configure identity providers (LDAP/AD).

  GET    /api/admin/auth-providers           list configs (secrets scrubbed)
  POST   /api/admin/auth-providers           create
  PUT    /api/admin/auth-providers/{id}      update
  DELETE /api/admin/auth-providers/{id}      delete
  POST   /api/admin/auth-providers/{id}/test test connectivity
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_db
from ...secrets.models import AuditLog
from ..dependencies import require_role
from ..models import IdentityProviderConfig, User

router = APIRouter()

# Keys inside config_json that we scrub from responses.
_SECRET_CONFIG_KEYS = {"bind_password", "password", "bind_dn_password",
                       "sp_private_key",   # SAML SP signing key
                       "client_secret"}    # OIDC client secret


class ProviderIn(BaseModel):
    provider_type: str = "ldap"
    provider_name: str = Field(min_length=1, max_length=100)
    config: dict = Field(default_factory=dict)
    group_role_mapping: dict[str, str] = Field(default_factory=dict)
    default_role: str = "user"
    auto_provision: bool = True
    is_enabled: bool = True
    is_default: bool = False


class ProviderUpdate(BaseModel):
    provider_name: str | None = None
    config: dict | None = None
    group_role_mapping: dict[str, str] | None = None
    default_role: str | None = None
    auto_provision: bool | None = None
    is_enabled: bool | None = None
    is_default: bool | None = None


def _scrub(config: dict) -> dict:
    return {k: ("***REDACTED***" if k.lower() in _SECRET_CONFIG_KEYS and v else v)
            for k, v in (config or {}).items()}


def _to_response(p: IdentityProviderConfig) -> dict:
    try:
        config = json.loads(p.config_json or "{}")
    except json.JSONDecodeError:
        config = {}
    try:
        gmap = json.loads(p.group_role_mapping or "{}")
    except json.JSONDecodeError:
        gmap = {}
    return {
        "id": p.id,
        "provider_type": p.provider_type,
        "provider_name": p.provider_name,
        "config": _scrub(config),
        "group_role_mapping": gmap,
        "default_role": p.default_role,
        "auto_provision": p.auto_provision,
        "is_enabled": p.is_enabled,
        "is_default": p.is_default,
        "created_at": p.created_at.isoformat(),
        "updated_at": p.updated_at.isoformat(),
    }


@router.get("")
async def list_providers(db: AsyncSession = Depends(get_db),
                         _u: User = Depends(require_role("admin"))):
    rows = (await db.execute(
        select(IdentityProviderConfig).order_by(IdentityProviderConfig.created_at)
    )).scalars().all()
    return [_to_response(p) for p in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_provider(body: ProviderIn, db: AsyncSession = Depends(get_db),
                          user: User = Depends(require_role("admin"))):
    p = IdentityProviderConfig(
        provider_type=body.provider_type,
        provider_name=body.provider_name,
        config_json=json.dumps(body.config),
        group_role_mapping=json.dumps(body.group_role_mapping),
        default_role=body.default_role,
        auto_provision=body.auto_provision,
        is_enabled=body.is_enabled,
        is_default=body.is_default,
    )
    db.add(p)
    await db.flush()
    db.add(AuditLog(user_id=user.id, action="auth_provider.create",
                    resource_type="auth_provider", resource_id=p.id,
                    details=f"Created {body.provider_type} provider '{body.provider_name}'"))
    await db.commit()
    await db.refresh(p)
    return _to_response(p)


@router.put("/{provider_id}")
async def update_provider(provider_id: str, body: ProviderUpdate,
                          db: AsyncSession = Depends(get_db),
                          user: User = Depends(require_role("admin"))):
    p = (await db.execute(
        select(IdentityProviderConfig).where(IdentityProviderConfig.id == provider_id)
    )).scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Provider not found")

    if body.provider_name is not None:
        p.provider_name = body.provider_name
    if body.config is not None:
        # Preserve existing secret values if the incoming config has the
        # redaction placeholder (UI sends back the scrubbed config on edit).
        existing = json.loads(p.config_json or "{}")
        merged = dict(body.config)
        for k in _SECRET_CONFIG_KEYS:
            if merged.get(k) == "***REDACTED***" and k in existing:
                merged[k] = existing[k]
        p.config_json = json.dumps(merged)
    if body.group_role_mapping is not None:
        p.group_role_mapping = json.dumps(body.group_role_mapping)
    if body.default_role is not None:
        p.default_role = body.default_role
    if body.auto_provision is not None:
        p.auto_provision = body.auto_provision
    if body.is_enabled is not None:
        p.is_enabled = body.is_enabled
    if body.is_default is not None:
        p.is_default = body.is_default

    db.add(AuditLog(user_id=user.id, action="auth_provider.update",
                    resource_type="auth_provider", resource_id=p.id,
                    details=f"Updated provider '{p.provider_name}'"))
    await db.commit()
    await db.refresh(p)
    return _to_response(p)


@router.delete("/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(provider_id: str, db: AsyncSession = Depends(get_db),
                          user: User = Depends(require_role("admin"))):
    p = (await db.execute(
        select(IdentityProviderConfig).where(IdentityProviderConfig.id == provider_id)
    )).scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Provider not found")
    db.add(AuditLog(user_id=user.id, action="auth_provider.delete",
                    resource_type="auth_provider", resource_id=p.id,
                    details=f"Deleted provider '{p.provider_name}'"))
    await db.delete(p)
    await db.commit()


@router.post("/{provider_id}/test")
async def test_provider(provider_id: str, db: AsyncSession = Depends(get_db),
                        _u: User = Depends(require_role("admin"))):
    p = (await db.execute(
        select(IdentityProviderConfig).where(IdentityProviderConfig.id == provider_id)
    )).scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Provider not found")
    if p.provider_type != "ldap":
        raise HTTPException(status_code=400, detail=f"Test not supported for {p.provider_type}")

    from .ldap_provider import LDAP3_AVAILABLE, LdapAuthProvider
    if not LDAP3_AVAILABLE:
        return {"success": False, "message": "ldap3 is not installed on this host. "
                                             "Install with: pip install ldap3"}
    try:
        config = json.loads(p.config_json or "{}")
    except json.JSONDecodeError:
        return {"success": False, "message": "Provider config is not valid JSON"}

    provider = LdapAuthProvider(config=config)
    ok, msg = provider.test_connection()
    return {"success": ok, "message": msg}
