import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


class FunctionCall(Base):
    """One invocation of an app server function — the Functions panel's
    "recent calls" feed. The audit log keeps the one-line outcome forever;
    this table keeps what a developer needs to debug (args, result, logs) for
    the most recent calls per app and is pruned, so it never grows unbounded."""

    __tablename__ = "app_function_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    app_id: Mapped[str] = mapped_column(String(36), index=True)
    fn_name: Mapped[str] = mapped_column(String(64), index=True)
    # Which tree ran: "draft" or "v<N>".
    source: Mapped[str] = mapped_column(String(20), default="draft")
    user_id: Mapped[str] = mapped_column(String(36), default="")
    # "app" = the running app called it through the SDK; "panel" = a
    # developer test-ran it from the Functions panel.
    trigger: Mapped[str] = mapped_column(String(10), default="app")
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    status_code: Mapped[int] = mapped_column(Integer, default=200)
    error: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    # Truncated JSON previews — enough to recognise a call, never a full dump.
    args_preview: Mapped[str] = mapped_column(Text, default="")
    result_preview: Mapped[str] = mapped_column(Text, default="")
    # stderr tail (ctx.log / print output), already capped by the runner.
    logs: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), index=True)
