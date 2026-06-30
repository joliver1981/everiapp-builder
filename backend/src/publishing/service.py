"""Publish-approval workflow: submit → review → approve/reject."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..apps.models import App
from ..auth.models import User
from ..secrets.models import AuditLog
from ..security_scan.service import GateDecision, evaluate_publish_gate
from ..versions.service import versions_service
from .models import PublishApproval


class ApprovalError(Exception):
    """Generic workflow error (not-found / wrong-state)."""


class SecurityBlocked(Exception):
    """Approval refused because the live scan blocks and no override was given."""

    def __init__(self, gate: GateDecision):
        super().__init__("security scan blocks publish")
        self.gate = gate


async def submit_request(db: AsyncSession, app_id: str, user: User, notes: str) -> PublishApproval:
    app = (await db.execute(select(App).where(App.id == app_id))).scalar_one_or_none()
    if not app:
        raise ApprovalError("App not found")

    # Snapshot the security posture so reviewers triage by risk.
    gate = await evaluate_publish_gate(db, app_id)
    rec = PublishApproval(
        app_id=app_id,
        requested_by=user.id,
        notes=notes or "",
        status="pending",
        security_max_severity=gate.report.max_severity,
        security_finding_count=len(gate.report.findings),
    )
    db.add(rec)
    await db.flush()  # populate rec.id before referencing it in the audit row
    db.add(AuditLog(
        user_id=user.id, action="app.publish.requested",
        resource_type="app", resource_id=app_id,
        details=f"Publish requested (request {rec.id}); "
                f"{rec.security_finding_count} security finding(s)",
    ))
    await db.commit()
    await db.refresh(rec)
    return rec


async def list_for_app(db: AsyncSession, app_id: str) -> list[PublishApproval]:
    return list((await db.execute(
        select(PublishApproval).where(PublishApproval.app_id == app_id)
        .order_by(PublishApproval.created_at.desc())
    )).scalars().all())


async def list_pending(db: AsyncSession) -> list[PublishApproval]:
    return list((await db.execute(
        select(PublishApproval).where(PublishApproval.status == "pending")
        .order_by(PublishApproval.created_at.asc())
    )).scalars().all())


async def get(db: AsyncSession, req_id: str) -> PublishApproval | None:
    return (await db.execute(
        select(PublishApproval).where(PublishApproval.id == req_id)
    )).scalar_one_or_none()


async def approve(db: AsyncSession, req_id: str, reviewer: User,
                  override_security: bool = False) -> PublishApproval:
    rec = await get(db, req_id)
    if not rec:
        raise ApprovalError("Request not found")
    if rec.status != "pending":
        raise ApprovalError(f"Request already {rec.status}")

    # Re-run the gate against the CURRENT code (it may have changed since request).
    gate = await evaluate_publish_gate(db, rec.app_id)
    if gate.blocked and not override_security:
        raise SecurityBlocked(gate)

    # Perform the real publish, crediting the original author.
    try:
        version = await versions_service.publish(db, rec.app_id, rec.requested_by, rec.notes)
    except ValueError as e:
        raise ApprovalError(str(e))

    rec.status = "approved"
    rec.reviewed_by = reviewer.id
    rec.reviewed_at = datetime.now(timezone.utc)
    rec.resulting_version = version.version
    db.add(AuditLog(
        user_id=reviewer.id, action="app.publish.approved",
        resource_type="app", resource_id=rec.app_id,
        details=f"Approved request {rec.id} -> v{version.version}"
                + (" (security override)" if gate.blocked else ""),
    ))
    await db.commit()
    await db.refresh(rec)
    return rec


async def reject(db: AsyncSession, req_id: str, reviewer: User, note: str) -> PublishApproval:
    rec = await get(db, req_id)
    if not rec:
        raise ApprovalError("Request not found")
    if rec.status != "pending":
        raise ApprovalError(f"Request already {rec.status}")

    rec.status = "rejected"
    rec.reviewed_by = reviewer.id
    rec.reviewed_at = datetime.now(timezone.utc)
    rec.review_note = note or ""
    db.add(AuditLog(
        user_id=reviewer.id, action="app.publish.rejected",
        resource_type="app", resource_id=rec.app_id,
        details=f"Rejected request {rec.id}: {note[:200]}",
    ))
    await db.commit()
    await db.refresh(rec)
    return rec
