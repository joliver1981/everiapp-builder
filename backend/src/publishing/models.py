import uuid
from datetime import datetime, timezone

from sqlalchemy import String, DateTime, ForeignKey, Text, Integer
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


class PublishApproval(Base):
    """A request to publish an app version, pending admin review.

    Created when `require_publish_approval` is on and a developer asks to
    publish. The `security_*` columns snapshot the scan posture at request time
    so a reviewer sees the risk without re-running anything.
    """
    __tablename__ = "publish_approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    app_id: Mapped[str] = mapped_column(String(36), ForeignKey("apps.id"), index=True)
    requested_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))
    notes: Mapped[str] = mapped_column(Text, default="")  # proposed release notes
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending|approved|rejected
    review_note: Mapped[str] = mapped_column(Text, default="")  # reviewer's comment
    reviewed_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    resulting_version: Mapped[int | None] = mapped_column(Integer, nullable=True)  # version made on approve
    security_max_severity: Mapped[str | None] = mapped_column(String(20), nullable=True)
    security_finding_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
