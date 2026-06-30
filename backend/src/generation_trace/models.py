"""One row per app-generation run — the full trace an admin/developer can inspect
to see exactly what the AI did and where it went wrong.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


class GenerationTrace(Base):
    __tablename__ = "generation_traces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    app_id: Mapped[str] = mapped_column(String(36), index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    user_id: Mapped[str] = mapped_column(String(64), default="", server_default="")
    user_message: Mapped[str] = mapped_column(Text, default="", server_default="")
    model: Mapped[str] = mapped_column(String(120), default="", server_default="")
    provider: Mapped[str] = mapped_column(String(60), default="", server_default="")
    # running | passed | failed | error | no_verify | no_files
    status: Mapped[str] = mapped_column(String(20), default="running", server_default="running")
    summary: Mapped[str] = mapped_column(Text, default="", server_default="")
    # JSON blobs (serialized in the service layer)
    system_prompts_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    steps_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    files_changed_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    verify_json: Mapped[str] = mapped_column(Text, default="null", server_default="null")
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    iterations: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), index=True
    )
