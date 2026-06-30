"""Tests for PII redaction utilities + LLM cost meter."""
from __future__ import annotations

import pytest


# --- PII redaction ----------------------------------------------------------
def test_redact_replaces_tagged_columns():
    from src.datasets.pii import REDACTED_PLACEHOLDER, redact_row
    row = {"id": 1, "name": "Alice", "email": "alice@example.com"}
    out = redact_row(row, ["email"])
    assert out["id"] == 1
    assert out["name"] == "Alice"
    assert out["email"] == REDACTED_PLACEHOLDER


def test_redact_preserves_untagged():
    from src.datasets.pii import redact_row
    row = {"id": 1, "name": "Alice"}
    out = redact_row(row, [])
    assert out == row


def test_redact_rows_handles_empty():
    from src.datasets.pii import redact_rows
    assert redact_rows([], ["email"]) == []


def test_pii_columns_extracts_names():
    from src.datasets.pii import pii_columns
    assert pii_columns({"email": "email_address", "ssn": "ssn"}) == ["email", "ssn"]
    assert pii_columns(None) == []


# --- LLM cost estimation ----------------------------------------------------
def test_cost_openai_gpt4o():
    from src.llm_usage.service import estimate_cost_usd
    # 1M input + 1M output at gpt-4o pricing (2.50 / 10.00 per 1M)
    c = estimate_cost_usd("openai", "gpt-4o", 1_000_000, 1_000_000)
    assert c == pytest.approx(12.50, rel=1e-3)


def test_cost_anthropic_sonnet():
    from src.llm_usage.service import estimate_cost_usd
    c = estimate_cost_usd("anthropic", "claude-sonnet-4-20250514", 1_000_000, 1_000_000)
    # sonnet: 3 + 15 = 18 per 1M each
    assert c == pytest.approx(18.0, rel=1e-3)


def test_cost_ollama_is_free():
    from src.llm_usage.service import estimate_cost_usd
    assert estimate_cost_usd("ollama", "llama3.3", 1_000_000, 1_000_000) == 0.0


def test_cost_unknown_provider_is_zero():
    from src.llm_usage.service import estimate_cost_usd
    assert estimate_cost_usd("nope", "huh", 1_000_000, 1_000_000) == 0.0


@pytest.mark.asyncio
async def test_record_and_summarize_usage():
    """Integration: record + query via a per-test in-memory engine, no global
    reloads (which we learned the hard way are flaky across files)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from src.database import Base
    from src.llm_usage.models import LLMUsage  # noqa: F401 — register model
    from src.llm_usage.service import (
        breakdown_by_user, record_usage, summary_last_n_days,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        # Bring up the whole model graph (LLMUsage depends only on Base).
        from src.auth.models import User, RefreshToken  # noqa: F401
        from src.apps.models import App, AppPermission, AppSetting, AppVersion, Conversation, Message  # noqa: F401
        from src.secrets.models import AuditLog, Secret  # noqa: F401
        from src.marketplace.models import MarketplaceListing  # noqa: F401
        from src.deployments.models import Deployment, DeploymentTarget  # noqa: F401
        from src.bug_reports.models import BugAnalysis, BugReport, FixAttempt  # noqa: F401
        from src.connections.models import Connection  # noqa: F401
        from src.datasets.models import AppDatasetBinding, Dataset  # noqa: F401
        await conn.run_sync(Base.metadata.create_all)

    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as s:
        await record_usage(s, user_id="u1", app_id="a1",
            provider_type="openai", model="gpt-4o", purpose="generation",
            input_tokens=10_000, output_tokens=2_000)
        await record_usage(s, user_id="u2", app_id="a1",
            provider_type="anthropic", model="claude-sonnet", purpose="generation",
            input_tokens=5_000, output_tokens=1_000)
        await record_usage(s, user_id="u1", app_id="a2",
            provider_type="ollama", model="llama3.3", purpose="ai_toggle",
            input_tokens=100_000, output_tokens=50_000)

    async with Session() as s:
        sumr = await summary_last_n_days(s, days=7)
        assert sumr.total_calls == 3
        assert sumr.total_input_tokens == 115_000
        assert sumr.total_output_tokens == 53_000
        assert sumr.total_cost_usd > 0

        by_user = await breakdown_by_user(s, days=7)
        assert len(by_user) == 2
        u1 = next(u for u in by_user if u["user_id"] == "u1")
        assert u1["calls"] == 2

    await engine.dispose()
