"""Read/write helpers for platform settings + budget enforcement helpers."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import PlatformSetting

# Defaults applied when a key has never been set.
DEFAULTS: dict[str, Any] = {
    "custom_system_prompt": "",
    "monthly_budget_usd": 0.0,        # 0 = unlimited
    "per_user_budget_usd": 0.0,       # 0 = unlimited
    "budget_alert_threshold": 0.8,    # warn at 80%
    # Security scan of generated code (see security_scan module)
    "security_scan_enabled": True,
    "security_scan_block_publish": True,
    "security_scan_block_severity": "high",  # info|low|medium|high|critical
    # AI runtime probe: render each generated build in headless Chromium to catch
    # mount/runtime errors tsc + build can't. OFF by default — it's slower and
    # needs Playwright + Chromium installed. When off, verify stops at boot.
    "runtime_probe_enabled": False,
    # Code-review approval gate before a published version goes live
    "require_publish_approval": False,
    # SIEM forwarding of audit events
    "siem_enabled": False,
    "siem_endpoint": "",              # https URL (HTTP push) or host:port (syslog)
    "siem_transport": "http",         # http | syslog
    "siem_auth_header": "",           # optional "Authorization: Bearer ..." value
    # Deployment auto-rollback on repeated health failures
    "auto_rollback_enabled": False,
    "auto_rollback_fail_threshold": 3,  # consecutive failed probes before rollback
    # Email / SMTP notifications
    "smtp_enabled": False,
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_username": "",
    "smtp_password": "",          # write-only: scrubbed from the settings GET response
    "smtp_use_tls": True,
    "notify_from": "",            # From address; falls back to smtp_username
    "notify_admin_emails": "",    # comma-separated; blank = all admin users with an email
    "notify_on_publish_request": True,
    "notify_on_deploy_failure": True,
    "notify_on_budget": True,
    "notify_on_bug_report": True,
    # First-run setup wizard — flips to True once an admin completes onboarding.
    "setup_completed": False,
    # Scheduled backups
    "backup_enabled": False,
    "backup_interval_hours": 24,
    "backup_retention": 7,         # keep the newest N backups
    # External AIHub Marketplace (public app gallery). Defaults to the hosted
    # EveriApp Marketplace so a fresh install can browse it out of the box;
    # override per-deployment with AIHUB_MARKETPLACE_URL or via Platform → Settings.
    # The API key stays blank by design — it is per-account, obtained from the
    # marketplace's Developer page, and never shipped with the product.
    "marketplace_url": os.environ.get("AIHUB_MARKETPLACE_URL", "https://aihub-marketplace.vercel.app"),
    "marketplace_api_key": "",     # write-only secret, like smtp_password
}

# Keys whose values are secrets — scrubbed from the admin GET response and
# preserved on PUT when the redacted placeholder is sent back.
SECRET_SETTING_KEYS = {"smtp_password", "marketplace_api_key"}


async def get_setting(db: AsyncSession, key: str) -> Any:
    row = (await db.execute(
        select(PlatformSetting).where(PlatformSetting.key == key)
    )).scalar_one_or_none()
    if row is None:
        return DEFAULTS.get(key)
    try:
        return json.loads(row.value_json)
    except json.JSONDecodeError:
        return DEFAULTS.get(key)


async def set_setting(db: AsyncSession, key: str, value: Any) -> None:
    row = (await db.execute(
        select(PlatformSetting).where(PlatformSetting.key == key)
    )).scalar_one_or_none()
    if row is None:
        row = PlatformSetting(key=key, value_json=json.dumps(value))
        db.add(row)
    else:
        row.value_json = json.dumps(value)
        row.updated_at = datetime.now(timezone.utc)
    await db.commit()


async def get_all(db: AsyncSession) -> dict[str, Any]:
    rows = (await db.execute(select(PlatformSetting))).scalars().all()
    out = dict(DEFAULTS)
    for r in rows:
        try:
            out[r.key] = json.loads(r.value_json)
        except json.JSONDecodeError:
            pass
    return out


# ---------------------------------------------------------------------------
# Budget enforcement
# ---------------------------------------------------------------------------
def _month_start() -> datetime:
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


class BudgetStatus:
    def __init__(self, *, allowed: bool, reason: str = "",
                 user_spent: float = 0.0, user_cap: float = 0.0,
                 org_spent: float = 0.0, org_cap: float = 0.0,
                 near_limit: bool = False):
        self.allowed = allowed
        self.reason = reason
        self.user_spent = user_spent
        self.user_cap = user_cap
        self.org_spent = org_spent
        self.org_cap = org_cap
        self.near_limit = near_limit

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed, "reason": self.reason,
            "user_spent": round(self.user_spent, 4), "user_cap": self.user_cap,
            "org_spent": round(self.org_spent, 4), "org_cap": self.org_cap,
            "near_limit": self.near_limit,
        }


async def check_budget(db: AsyncSession, user_id: str) -> BudgetStatus:
    """Return whether `user_id` may make another LLM call this month.

    Enforces both the per-user cap and the org-wide cap (0 = unlimited for
    either). Also flags `near_limit` when spend crosses the alert threshold.
    """
    from ..llm_usage.service import user_cost_in_window
    from ..llm_usage.models import LLMUsage
    from sqlalchemy import func

    settings_map = await get_all(db)
    user_cap = float(settings_map.get("per_user_budget_usd") or 0.0)
    org_cap = float(settings_map.get("monthly_budget_usd") or 0.0)
    threshold = float(settings_map.get("budget_alert_threshold") or 0.8)

    month_start = _month_start()
    user_spent = await user_cost_in_window(db, user_id, month_start)

    org_spent = float((await db.execute(
        select(func.coalesce(func.sum(LLMUsage.cost_usd), 0.0))
        .where(LLMUsage.created_at >= month_start)
    )).scalar_one() or 0.0)

    near_limit = False
    if user_cap > 0 and user_spent >= user_cap:
        return BudgetStatus(allowed=False,
                            reason=f"Per-user monthly budget of ${user_cap:.2f} exceeded "
                                   f"(spent ${user_spent:.2f})",
                            user_spent=user_spent, user_cap=user_cap,
                            org_spent=org_spent, org_cap=org_cap)
    if org_cap > 0 and org_spent >= org_cap:
        return BudgetStatus(allowed=False,
                            reason=f"Org monthly budget of ${org_cap:.2f} exceeded "
                                   f"(spent ${org_spent:.2f})",
                            user_spent=user_spent, user_cap=user_cap,
                            org_spent=org_spent, org_cap=org_cap)

    if user_cap > 0 and user_spent >= user_cap * threshold:
        near_limit = True
    if org_cap > 0 and org_spent >= org_cap * threshold:
        near_limit = True

    return BudgetStatus(allowed=True, user_spent=user_spent, user_cap=user_cap,
                        org_spent=org_spent, org_cap=org_cap, near_limit=near_limit)
