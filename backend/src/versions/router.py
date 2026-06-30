from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..auth.dependencies import require_role
from ..auth.models import User
from ..secrets.models import AuditLog
from ..security_scan.service import evaluate_publish_gate
from ..platform_settings.service import get_setting
from .schemas import PublishRequest, VersionResponse
from .service import versions_service

router = APIRouter()


@router.get("/{app_id}/versions", response_model=list[VersionResponse])
async def list_versions(
    app_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    versions = await versions_service.list_versions(db, app_id)
    return [
        VersionResponse(
            id=v.id, app_id=v.app_id, version=v.version, notes=v.notes,
            published_by=v.published_by, manifest=v.manifest or {},
            created_at=v.created_at.isoformat(),
        )
        for v in versions
    ]


@router.get("/{app_id}/versions/diff")
async def diff_versions(
    app_id: str,
    from_ref: str = Query(..., alias="from", description="version number or 'draft'"),
    to_ref: str = Query(..., alias="to", description="version number or 'draft'"),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("admin", "developer")),
):
    """Unified diff between two versions (or a version and the live draft)."""
    for ref in (from_ref, to_ref):
        if ref != "draft" and not ref.isdigit():
            raise HTTPException(status_code=400,
                                detail="Refs must be a version number or 'draft'")
    try:
        return await versions_service.diff_versions(app_id, from_ref, to_ref)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{app_id}/versions", response_model=VersionResponse, status_code=201)
async def publish_version(
    app_id: str,
    body: PublishRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    # --- Approval gate: when review is required, developers must submit a
    # publish request instead of publishing directly. Admins (the reviewers)
    # may still publish directly.
    if user.role != "admin" and bool(await get_setting(db, "require_publish_approval")):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "approval_required",
                "message": "Publishing requires admin approval. Submit a publish "
                           "request at POST /api/apps/{app_id}/publish-requests.",
            },
        )

    # --- Security gate: scan the draft before it can become a version -------
    gate = await evaluate_publish_gate(db, app_id)
    if gate.blocked:
        if not (body.override_security and user.role == "admin"):
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "security_scan_blocked",
                    "message": (
                        f"Publishing is blocked: {len(gate.blocking)} security "
                        f"finding(s) at or above '{gate.threshold}'. An admin can "
                        f"override after review."
                    ),
                    **gate.to_dict(),
                },
            )
        # Admin chose to override — record it before the snapshot is taken so the
        # override is captured in the same transaction as the publish.
        db.add(AuditLog(
            user_id=user.id,
            action="app.publish.security_override",
            resource_type="app",
            resource_id=app_id,
            details=(
                f"Override: published despite {len(gate.blocking)} "
                f"'{gate.threshold}'+ finding(s) "
                f"[{', '.join(sorted({f.rule_id for f in gate.blocking}))}]"
            ),
        ))

    try:
        version = await versions_service.publish(db, app_id, user.id, body.notes)
        return VersionResponse(
            id=version.id, app_id=version.app_id, version=version.version,
            notes=version.notes, published_by=version.published_by,
            manifest=version.manifest or {}, created_at=version.created_at.isoformat(),
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{app_id}/versions/{version}/rollback", response_model=VersionResponse)
async def rollback_version(
    app_id: str,
    version: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    try:
        new_version = await versions_service.rollback(db, app_id, version, user.id)
        return VersionResponse(
            id=new_version.id, app_id=new_version.app_id, version=new_version.version,
            notes=new_version.notes, published_by=new_version.published_by,
            manifest=new_version.manifest or {}, created_at=new_version.created_at.isoformat(),
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
