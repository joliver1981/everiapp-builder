"""On-demand security-scan endpoint for the app builder."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_role
from ..auth.models import User
from ..database import get_db
from .scanner import scan_app
from .service import evaluate_publish_gate

router = APIRouter()


@router.post("/{app_id}/security-scan")
async def run_security_scan(
    app_id: str,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("admin", "developer")),
):
    """Scan the app's current draft and return findings + whether they'd block a publish."""
    gate = await evaluate_publish_gate(db, app_id)
    return gate.to_dict()


@router.get("/{app_id}/security-scan")
async def get_security_scan(
    app_id: str,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("admin", "developer")),
):
    """Same as POST — a plain report, convenient for a builder panel to poll."""
    return scan_app(app_id).to_dict()
