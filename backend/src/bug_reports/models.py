import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


# Statuses BugReport flows through
# - new          : just received, not yet analyzed
# - analyzing    : LLM call in progress
# - analyzed     : analysis ready, waiting for human approval
# - approved     : approved (by human or auto-approve), fix in flight
# - applying    : applying file changes / publishing version / building
# - testing      : npm run build in progress
# - deploying    : pushing to target
# - resolved     : fix successfully deployed
# - rejected     : human rejected the suggested fix
# - failed       : something blew up; see error column
ALL_STATUSES = (
    "new", "analyzing", "analyzed", "approved",
    "applying", "testing", "deploying", "resolved",
    "rejected", "failed",
)

RISK_LEVELS = ("low", "medium", "high")


class BugReport(Base):
    __tablename__ = "bug_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    app_id: Mapped[str] = mapped_column(String(36), ForeignKey("apps.id"), index=True)
    version: Mapped[int | None] = mapped_column(Integer, nullable=True)  # version that was running when reported
    deployment_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("deployments.id"), nullable=True)
    reporter_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # may be null for anon
    reporter_label: Mapped[str | None] = mapped_column(String(200), nullable=True)  # name/email if anon

    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")

    # Auto-captured context: page_url, user_agent, viewport, console_tail, network_errors, etc.
    captured_context: Mapped[dict] = mapped_column(JSON, default=dict)
    screenshot_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class BugAnalysis(Base):
    __tablename__ = "bug_analyses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    bug_report_id: Mapped[str] = mapped_column(String(36), ForeignKey("bug_reports.id"), index=True)

    diagnosis: Mapped[str] = mapped_column(Text, default="")
    root_cause: Mapped[str] = mapped_column(Text, default="")
    proposed_files: Mapped[list] = mapped_column(JSON, default=list)
    # Each entry: {path: str, action: "create"|"update"|"delete", content: str, current_content: str|None}

    risk_level: Mapped[str] = mapped_column(String(10), default="medium")  # low | medium | high
    risk_rationale: Mapped[str] = mapped_column(Text, default="")

    llm_provider_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)  # for debugging

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class FixAttempt(Base):
    __tablename__ = "fix_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    bug_report_id: Mapped[str] = mapped_column(String(36), ForeignKey("bug_reports.id"), index=True)
    analysis_id: Mapped[str] = mapped_column(String(36), ForeignKey("bug_analyses.id"))
    base_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    new_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deployment_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("deployments.id"), nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="pending")
    # pending | applying | building | deploying | succeeded | failed | rejected
    auto_approved: Mapped[bool] = mapped_column(default=False)
    approved_by: Mapped[str | None] = mapped_column(String(36), nullable=True)  # user id
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
