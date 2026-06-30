"""Publish-approval endpoints (app-scoped) + the global review queue (admin)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_role
from ..auth.models import User
from ..database import get_db
from ..notifications import service as notify
from . import service
from .schemas import ApprovalResponse, ReviewRequest, SubmitPublishRequest

# Mounted at /api/apps
router = APIRouter()
# Mounted at /api/admin
admin_router = APIRouter()


@router.post("/{app_id}/publish-requests", response_model=ApprovalResponse, status_code=201)
async def submit_publish_request(
    app_id: str,
    body: SubmitPublishRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    try:
        rec = await service.submit_request(db, app_id, user, body.notes)
    except service.ApprovalError as e:
        raise HTTPException(status_code=404, detail=str(e))
    await notify.notify_publish_requested(db, app_id, user.display_name or user.username, rec.id)
    return ApprovalResponse.of(rec)


@router.get("/{app_id}/publish-requests", response_model=list[ApprovalResponse])
async def list_app_publish_requests(
    app_id: str,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("admin", "developer")),
):
    return [ApprovalResponse.of(r) for r in await service.list_for_app(db, app_id)]


@router.post("/{app_id}/publish-requests/{req_id}/approve", response_model=ApprovalResponse)
async def approve_publish_request(
    app_id: str,
    req_id: str,
    body: ReviewRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    try:
        rec = await service.approve(db, req_id, user, override_security=body.override_security)
    except service.SecurityBlocked as e:
        raise HTTPException(status_code=422, detail={
            "error": "security_scan_blocked",
            "message": "The current code trips the security scan. Approve with "
                       "override_security=true after reviewing the findings.",
            **e.gate.to_dict(),
        })
    except service.ApprovalError as e:
        raise HTTPException(status_code=409, detail=str(e))
    await notify.notify_publish_decided(db, app_id, rec.requested_by, "approved")
    return ApprovalResponse.of(rec)


@router.post("/{app_id}/publish-requests/{req_id}/reject", response_model=ApprovalResponse)
async def reject_publish_request(
    app_id: str,
    req_id: str,
    body: ReviewRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    try:
        rec = await service.reject(db, req_id, user, body.review_note)
    except service.ApprovalError as e:
        raise HTTPException(status_code=409, detail=str(e))
    await notify.notify_publish_decided(db, app_id, rec.requested_by, "rejected", body.review_note)
    return ApprovalResponse.of(rec)


@admin_router.get("/publish-requests", response_model=list[ApprovalResponse])
async def list_pending_publish_requests(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("admin")),
):
    """Global queue of pending publish requests for the review dashboard."""
    return [ApprovalResponse.of(r) for r in await service.list_pending(db)]
