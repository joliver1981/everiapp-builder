"""ai_spans — one row per traced operation (Phase 1: ai.call only).

Payloads (prompt/response) are stored Fernet-encrypted, or not at all,
depending on the trace_capture_level platform setting at write time. Spans are
debug data with short retention — the billing ledger stays in llm_usage
(joined via llm_usage.trace_id/span_id, deliberately not merged).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


class AISpan(Base):
    __tablename__ = "ai_spans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    parent_span_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    app_id: Mapped[str] = mapped_column(String(36), index=True, default="")
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    kind: Mapped[str] = mapped_column(String(30), default="ai.call")
    purpose: Mapped[str] = mapped_column(String(50), default="")
    # Semantic name of the call site (becomes the decision name when aiDecide lands).
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    provider_type: Mapped[str] = mapped_column(String(50), default="")
    model: Mapped[str] = mapped_column(String(100), default="")
    status: Mapped[str] = mapped_column(String(10), default="ok")  # ok | error
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Fernet ciphertext; NULL when capture level stripped the payload.
    prompt_ct: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_ct: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The level that was applied when this row was written (full | metadata_only).
    capture_level: Mapped[str] = mapped_column(String(20), default="metadata_only")

    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), index=True
    )
