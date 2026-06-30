"""LLM usage tracking — token counts + cost estimates per call.

Persisted in `llm_usage` table; queried by the admin cost dashboard and
the per-user budget enforcement layer.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


class LLMUsage(Base):
    __tablename__ = "llm_usage"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    app_id: Mapped[str] = mapped_column(String(36), index=True)
    provider_type: Mapped[str] = mapped_column(String(50))   # openai / anthropic / ollama / ...
    model: Mapped[str] = mapped_column(String(100))
    purpose: Mapped[str] = mapped_column(String(50))         # generation / verify / ai_toggle
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), index=True
    )
