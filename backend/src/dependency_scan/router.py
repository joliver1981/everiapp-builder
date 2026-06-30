"""On-demand dependency scan for the app builder."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..auth.dependencies import require_role
from ..auth.models import User
from .scanner import scan_dependencies

router = APIRouter()


@router.get("/{app_id}/dependency-scan")
async def dependency_scan(app_id: str, _user: User = Depends(require_role("admin", "developer"))):
    return scan_dependencies(app_id)
