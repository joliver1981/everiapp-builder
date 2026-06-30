"""HTTP endpoints for the license system.

  GET  /api/admin/license            → current license info
  POST /api/admin/license            → install a new license key (JWT body)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth.dependencies import require_role
from ..auth.models import User
from . import license as lic

router = APIRouter()


class LicenseTokenIn(BaseModel):
    token: str


@router.get("")
async def get_license(_user: User = Depends(require_role("admin"))) -> dict:
    return lic.current_license().to_dict()


@router.post("")
async def install_license(
    body: LicenseTokenIn,
    _user: User = Depends(require_role("admin")),
) -> dict:
    """Parse + validate a license JWT. On success, persist it under
    data/license.key so the next restart picks it up automatically."""
    info = lic.parse_license_token(body.token)
    if not info.is_active:
        raise HTTPException(
            status_code=400,
            detail=f"License rejected: {info.issue or info.status}",
        )

    # Persist to data/license.key for restart-persistence
    from pathlib import Path
    from ..config import settings

    key_path = Path(settings.app_data_dir) / "license.key"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text(body.token, encoding="utf-8")

    lic.set_current_license(info)
    return info.to_dict()
