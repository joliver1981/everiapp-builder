"""Admin routes for server-function Python packages.

Mounted at /api/admin/python-packages. Everything here is admin-only: package
installs change what every app's server functions can import.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_role
from ..auth.models import User
from ..database import get_db
from . import service
from .service import PackageError

router = APIRouter()


class _InstallIn(BaseModel):
    name: str
    version: str | None = None


@router.get("")
async def list_packages(
    db: AsyncSession = Depends(get_db),
    _u: User = Depends(require_role("admin")),
):
    """Everything server functions can import: bundled set + admin installs,
    plus the environment (interpreter, pip availability, managed dir)."""
    return await service.list_inventory(db)


@router.get("/lookup")
async def lookup_package(
    name: str,
    db: AsyncSession = Depends(get_db),
    _u: User = Depends(require_role("admin")),
):
    """Best-effort version lookup (PyPI JSON API, or PEP 691 against the
    configured index). Always 200 with {available: false, error} on lookup
    trouble — install works from a typed version even when lookup can't."""
    try:
        return await service.lookup(db, name)
    except PackageError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


@router.post("", status_code=202)
async def install_package(
    body: _InstallIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    try:
        return await service.start_install(db, body.name, body.version, user)
    except PackageError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


@router.delete("/{name}", status_code=202)
async def uninstall_package(
    name: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    try:
        return await service.start_uninstall(db, name, user)
    except PackageError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


@router.post("/rebuild", status_code=202)
async def rebuild_environment(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    """Re-install the whole manifest into a fresh directory (one pip run =
    real dependency resolution) — the recovery path for additive-install
    drift or a broken environment."""
    try:
        await service.start_rebuild(db, user)
        return {"started": True}
    except PackageError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
