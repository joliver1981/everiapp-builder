"""Admin endpoints for platform settings (custom prompt, budgets)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_role
from ..auth.models import User
from ..database import get_db
from . import service

router = APIRouter()


class SettingsIn(BaseModel):
    custom_system_prompt: str | None = None
    monthly_budget_usd: float | None = None
    per_user_budget_usd: float | None = None
    budget_alert_threshold: float | None = None
    security_scan_enabled: bool | None = None
    security_scan_block_publish: bool | None = None
    security_scan_block_severity: str | None = None
    runtime_probe_enabled: bool | None = None
    require_publish_approval: bool | None = None
    siem_enabled: bool | None = None
    siem_endpoint: str | None = None
    siem_transport: str | None = None
    siem_auth_header: str | None = None
    auto_rollback_enabled: bool | None = None
    auto_rollback_fail_threshold: int | None = None
    smtp_enabled: bool | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool | None = None
    notify_from: str | None = None
    notify_admin_emails: str | None = None
    notify_on_publish_request: bool | None = None
    notify_on_deploy_failure: bool | None = None
    notify_on_budget: bool | None = None
    notify_on_bug_report: bool | None = None
    backup_enabled: bool | None = None
    backup_interval_hours: int | None = None
    backup_retention: int | None = None
    marketplace_url: str | None = None
    marketplace_api_key: str | None = None


_REDACTED = "***REDACTED***"


def _scrub_secrets(values: dict) -> dict:
    out = dict(values)
    for k in service.SECRET_SETTING_KEYS:
        if out.get(k):
            out[k] = _REDACTED
    return out


@router.get("")
async def get_settings(db: AsyncSession = Depends(get_db),
                       _u: User = Depends(require_role("admin"))):
    return _scrub_secrets(await service.get_all(db))


@router.put("")
async def update_settings(body: SettingsIn, db: AsyncSession = Depends(get_db),
                          _u: User = Depends(require_role("admin"))):
    for key, val in body.model_dump(exclude_none=True).items():
        # Preserve a stored secret when the UI sends back the redacted placeholder.
        if key in service.SECRET_SETTING_KEYS and val == _REDACTED:
            continue
        await service.set_setting(db, key, val)
    return _scrub_secrets(await service.get_all(db))


@router.get("/budget-status")
async def budget_status(db: AsyncSession = Depends(get_db),
                        user: User = Depends(require_role("admin"))):
    """Current month's budget state for the calling admin (used by the cost UI)."""
    status = await service.check_budget(db, user.id)
    return status.to_dict()
