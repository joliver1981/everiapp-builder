"""LLM usage tracking service — record calls + aggregate summaries.

The token-cost table is a simplification; you'll want to keep it updated as
provider pricing changes. Values here in USD per 1M tokens (input / output).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import LLMUsage

# Per-1M-token pricing in USD. Keep updated; missing entries default to 0.
# Source: provider docs as of mid-2026. Always verify against current pricing.
_PRICE_TABLE: dict[tuple[str, str], tuple[float, float]] = {
    # (provider_type, model_substring) → (input_per_1m, output_per_1m).
    # estimate_cost_usd matches the FIRST substring CONTAINED in the model id, in
    # this insertion order — so more-specific families MUST precede more-general
    # ones (e.g. "gpt-5.4-mini" before "gpt-5.4"), and the empty-string Ollama
    # entry (matches anything) MUST stay last.
    # --- OpenAI (current GPT-5.x; gpt-4o kept for already-configured providers) -
    ("openai", "gpt-5.4-nano"):          (0.20, 1.25),
    ("openai", "gpt-5.4-mini"):          (0.75, 4.50),
    ("openai", "gpt-5.5"):               (5.00, 30.00),
    ("openai", "gpt-5.4"):               (2.50, 15.00),
    ("openai", "gpt-4o-mini"):           (0.15, 0.60),
    ("openai", "gpt-4o"):                (2.50, 10.00),
    # --- Azure OpenAI (same model families) ---
    ("azure",  "gpt-5.4-nano"):          (0.20, 1.25),
    ("azure",  "gpt-5.4-mini"):          (0.75, 4.50),
    ("azure",  "gpt-5.5"):               (5.00, 30.00),
    ("azure",  "gpt-5.4"):               (2.50, 15.00),
    ("azure",  "gpt-4o"):                (2.50, 10.00),
    # --- Anthropic (fable is its own family; opus/sonnet/haiku match any dated id) -
    ("anthropic", "claude-fable-5"):     (10.00, 50.00),
    ("anthropic", "claude-opus"):        (5.00, 25.00),
    ("anthropic", "claude-sonnet"):      (3.00, 15.00),
    ("anthropic", "claude-haiku"):       (1.00, 5.00),
    # --- Google Gemini (flash-lite before the gemini-3.1 pro prefix) ---
    ("google", "gemini-3.1-flash-lite"): (0.25, 1.50),
    ("google", "gemini-3.1-pro"):        (2.00, 12.00),
    ("google", "gemini-3.5-flash"):      (1.50, 9.00),
    ("google", "gemini-2.5-pro"):        (1.25, 10.00),
    ("google", "gemini-2.5-flash"):      (0.30, 2.50),
    ("ollama", ""):                      (0.0, 0.0),  # self-hosted = free (keep last)
}


def estimate_cost_usd(provider_type: str, model: str, input_tokens: int, output_tokens: int) -> float:
    """Look up unit price by (provider_type, model-substring match) and compute USD cost."""
    pt = (provider_type or "").lower()
    m = (model or "").lower()
    in_price, out_price = 0.0, 0.0
    # Try exact-ish match first, then substring fallback
    for (k_provider, k_model), (i, o) in _PRICE_TABLE.items():
        if k_provider != pt:
            continue
        if not k_model or k_model in m:
            in_price, out_price = i, o
            break
    return round(
        (input_tokens * in_price / 1_000_000) + (output_tokens * out_price / 1_000_000),
        6,
    )


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------
async def record_usage(
    db: AsyncSession,
    *,
    user_id: str,
    app_id: str,
    provider_type: str,
    model: str,
    purpose: str,
    input_tokens: int,
    output_tokens: int,
    error: str | None = None,
) -> LLMUsage:
    total = (input_tokens or 0) + (output_tokens or 0)
    cost = estimate_cost_usd(provider_type, model, input_tokens, output_tokens)
    row = LLMUsage(
        user_id=user_id,
        app_id=app_id,
        provider_type=provider_type,
        model=model,
        purpose=purpose,
        input_tokens=input_tokens or 0,
        output_tokens=output_tokens or 0,
        total_tokens=total,
        cost_usd=cost,
        error=error,
    )
    db.add(row)
    await db.commit()
    return row


# ---------------------------------------------------------------------------
# Aggregations for the admin cost dashboard
# ---------------------------------------------------------------------------
@dataclass
class UsageSummary:
    total_calls: int
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: float
    error_count: int


async def summary_last_n_days(db: AsyncSession, days: int) -> UsageSummary:
    """Aggregate usage over the trailing N days."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    r = (await db.execute(
        select(
            func.count(LLMUsage.id),
            func.coalesce(func.sum(LLMUsage.input_tokens), 0),
            func.coalesce(func.sum(LLMUsage.output_tokens), 0),
            func.coalesce(func.sum(LLMUsage.cost_usd), 0.0),
            func.sum(case((LLMUsage.error.isnot(None), 1), else_=0)),
        ).where(LLMUsage.created_at >= since)
    )).one()
    return UsageSummary(
        total_calls=int(r[0] or 0),
        total_input_tokens=int(r[1] or 0),
        total_output_tokens=int(r[2] or 0),
        total_cost_usd=float(r[3] or 0.0),
        error_count=int(r[4] or 0),
    )


async def breakdown_by_user(db: AsyncSession, days: int) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await db.execute(
        select(
            LLMUsage.user_id,
            func.count(LLMUsage.id),
            func.coalesce(func.sum(LLMUsage.total_tokens), 0),
            func.coalesce(func.sum(LLMUsage.cost_usd), 0.0),
        )
        .where(LLMUsage.created_at >= since)
        .group_by(LLMUsage.user_id)
        .order_by(func.sum(LLMUsage.cost_usd).desc())
    )).all()
    return [
        {"user_id": r[0], "calls": int(r[1]), "tokens": int(r[2]), "cost_usd": round(float(r[3]), 4)}
        for r in rows
    ]


async def breakdown_by_app(db: AsyncSession, days: int) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await db.execute(
        select(
            LLMUsage.app_id,
            func.count(LLMUsage.id),
            func.coalesce(func.sum(LLMUsage.total_tokens), 0),
            func.coalesce(func.sum(LLMUsage.cost_usd), 0.0),
        )
        .where(LLMUsage.created_at >= since)
        .group_by(LLMUsage.app_id)
        .order_by(func.sum(LLMUsage.cost_usd).desc())
    )).all()
    return [
        {"app_id": r[0], "calls": int(r[1]), "tokens": int(r[2]), "cost_usd": round(float(r[3]), 4)}
        for r in rows
    ]


async def user_cost_in_window(
    db: AsyncSession, user_id: str, since: datetime
) -> float:
    """Used by the budget check — total USD for one user since a date."""
    r = (await db.execute(
        select(func.coalesce(func.sum(LLMUsage.cost_usd), 0.0))
        .where(LLMUsage.user_id == user_id, LLMUsage.created_at >= since)
    )).scalar_one()
    return float(r or 0.0)
