"""Platform-wide settings — admin-tunable key/value store.

A single-row-per-key table holding JSON values. Used for:
  - custom_system_prompt   (str): appended to the AI generation system prompt
  - monthly_budget_usd     (float): org-wide LLM spend cap per calendar month
  - per_user_budget_usd    (float): per-user monthly cap (0 = unlimited)
  - budget_alert_threshold (float): 0..1 fraction at which to warn

We keep this as a flexible KV store rather than columns so adding a new tunable
is a code-only change (no migration).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


class PlatformSetting(Base):
    __tablename__ = "platform_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text, default="null")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
