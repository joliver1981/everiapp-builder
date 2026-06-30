from __future__ import annotations

from pydantic import BaseModel

from .models import PublishApproval


class SubmitPublishRequest(BaseModel):
    notes: str = ""


class ReviewRequest(BaseModel):
    review_note: str = ""
    # Used by approve only: publish even though the security scan blocks.
    override_security: bool = False


class ApprovalResponse(BaseModel):
    id: str
    app_id: str
    requested_by: str
    notes: str
    status: str
    review_note: str
    reviewed_by: str | None
    resulting_version: int | None
    security_max_severity: str | None
    security_finding_count: int
    created_at: str
    reviewed_at: str | None

    @classmethod
    def of(cls, r: PublishApproval) -> "ApprovalResponse":
        return cls(
            id=r.id, app_id=r.app_id, requested_by=r.requested_by, notes=r.notes,
            status=r.status, review_note=r.review_note, reviewed_by=r.reviewed_by,
            resulting_version=r.resulting_version,
            security_max_severity=r.security_max_severity,
            security_finding_count=r.security_finding_count,
            created_at=r.created_at.isoformat(),
            reviewed_at=r.reviewed_at.isoformat() if r.reviewed_at else None,
        )
