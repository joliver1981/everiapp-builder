"""Setup-wizard status + completion."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_role
from ..auth.models import IdentityProviderConfig, User
from ..database import get_db
from ..platform_settings.service import get_all, get_setting, set_setting

router = APIRouter()


@router.get("/status")
async def setup_status(db: AsyncSession = Depends(get_db)):
    """Public — reveals whether onboarding is still needed and, crucially,
    whether NO admin account exists yet (a fresh install). `needs_admin` drives
    the first-run 'create your admin account' screen."""
    from ..auth.service import auth_service
    return {
        "needs_setup": not bool(await get_setting(db, "setup_completed")),
        "needs_admin": not await auth_service.admin_exists(db),
    }


@router.get("/state")
async def setup_state(db: AsyncSession = Depends(get_db),
                      _u: User = Depends(require_role("admin"))):
    """Per-step completion, to drive the wizard's checklist."""
    cfg = await get_all(db)
    providers = (await db.execute(
        select(func.count(IdentityProviderConfig.id)).where(
            IdentityProviderConfig.is_enabled == True)  # noqa: E712
    )).scalar_one()
    return {
        "needs_setup": not bool(cfg.get("setup_completed")),
        "setup_completed": bool(cfg.get("setup_completed")),
        "has_identity_provider": int(providers or 0) > 0,
        "smtp_configured": bool(cfg.get("smtp_enabled")) and bool(cfg.get("smtp_host")),
        "has_custom_prompt": bool((cfg.get("custom_system_prompt") or "").strip()),
        "budgets_set": float(cfg.get("monthly_budget_usd") or 0) > 0
                       or float(cfg.get("per_user_budget_usd") or 0) > 0,
    }


@router.post("/complete")
async def setup_complete(db: AsyncSession = Depends(get_db),
                         _u: User = Depends(require_role("admin"))):
    await set_setting(db, "setup_completed", True)
    return {"ok": True, "setup_completed": True}
