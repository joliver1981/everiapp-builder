"""Decisions — named mini-LLM calls as first-class platform resources.

A decision is a CONTRACT (name + output schema + fallback) whose prompt lives
server-side as data: the generated app's call site carries only the name and
an input object (`aiDecide('classify_question', {question})`), so admins can
edit the prompt in the platform and the next invocation uses it — no rebuild,
no redeploy. Same inversion as datasets (call sites never contain SQL).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


def _now():
    return datetime.now(timezone.utc)


class AppDecision(Base):
    __tablename__ = "app_decisions"
    __table_args__ = (UniqueConstraint("app_id", "name", name="uq_app_decision_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    app_id: Mapped[str] = mapped_column(String(36), ForeignKey("apps.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    # Human phrasing for story mode ("is this a follow-up question?").
    description: Mapped[str] = mapped_column(String(300), default="")
    prompt_template: Mapped[str] = mapped_column(Text)
    # JSON Schema for the result. Minimal enforcement today (enum/type);
    # violations resolve to the fallback, never to a raised error.
    output_schema_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # MANDATORY: deployed apps depend on platform reachability for every
    # decision — the declared fallback is how that dependency stays safe.
    fallback_json: Mapped[str] = mapped_column(Text)  # json.dumps of any value
    # Optional per-decision model override (provider comes from the
    # purpose="decision" pin, inheriting the generation default when unset).
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    temperature: Mapped[float] = mapped_column(Float, default=0.0)
    cache_ttl_seconds: Mapped[int] = mapped_column(Integer, default=0)  # 0 = off

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class DecisionCache(Base):
    """Exact-match result cache. Keys include the prompt hash (edits
    invalidate) and the calling user (results never leak across users)."""
    __tablename__ = "decision_cache"
    __table_args__ = (UniqueConstraint("decision_id", "cache_key", name="uq_decision_cache_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    decision_id: Mapped[str] = mapped_column(String(36), ForeignKey("app_decisions.id"), index=True)
    cache_key: Mapped[str] = mapped_column(String(64), index=True)
    value_json: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
