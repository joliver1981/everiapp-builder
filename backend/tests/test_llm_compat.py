"""The litellm compat shim: retry-without-temperature for models that reject it.

Reproduces the real failure a user hit after adding a Claude provider:
    litellm.BadRequestError: AnthropicException -
    {"message":"`temperature` is deprecated for this model."}
The shim must drop `temperature` and retry instead of failing the turn.
"""
from __future__ import annotations

import asyncio
import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "llm-compat-test")

import litellm  # noqa: E402
from src import llm_compat  # noqa: E402

_DEPRECATION = (
    "litellm.BadRequestError: AnthropicException - "
    '{"type":"error","error":{"type":"invalid_request_error",'
    '"message":"`temperature` is deprecated for this model."}}'
)


def test_passthrough_returns_result_and_enables_drop_params(monkeypatch):
    calls = []

    async def fake(**kwargs):
        calls.append(dict(kwargs))
        return "OK"

    monkeypatch.setattr(litellm, "acompletion", fake)
    out = asyncio.run(llm_compat.acompletion(model="gpt-4o", temperature=0.7))
    assert out == "OK"
    assert len(calls) == 1                 # no retry on success
    assert calls[0]["temperature"] == 0.7  # param forwarded untouched
    assert litellm.drop_params is True     # litellm asked to strip known-bad params


def test_retries_without_temperature_on_deprecation(monkeypatch):
    calls = []

    async def fake(**kwargs):
        calls.append(dict(kwargs))
        if "temperature" in kwargs:
            raise Exception(_DEPRECATION)
        return "GENERATED"

    monkeypatch.setattr(litellm, "acompletion", fake)
    out = asyncio.run(llm_compat.acompletion(
        model="anthropic/claude-newest", messages=[{"role": "user", "content": "hi"}],
        temperature=0.7, max_tokens=16384, stream=True,
    ))
    assert out == "GENERATED"
    assert len(calls) == 2                       # one failure, one successful retry
    assert "temperature" in calls[0]
    assert "temperature" not in calls[1]         # offending param dropped
    assert calls[1]["max_tokens"] == 16384       # all other kwargs preserved
    assert calls[1]["stream"] is True


def test_unrelated_error_is_reraised(monkeypatch):
    async def fake(**kwargs):
        raise Exception("RateLimitError: too many requests")

    monkeypatch.setattr(litellm, "acompletion", fake)
    with pytest.raises(Exception, match="RateLimitError"):
        asyncio.run(llm_compat.acompletion(model="x", temperature=0.7))


def test_temperature_error_but_no_temperature_kwarg_reraises(monkeypatch):
    # If we can't drop the named param (it isn't in kwargs), don't loop — re-raise.
    async def fake(**kwargs):
        raise Exception(_DEPRECATION)

    monkeypatch.setattr(litellm, "acompletion", fake)
    with pytest.raises(Exception, match="deprecated"):
        asyncio.run(llm_compat.acompletion(model="x", max_tokens=10))


# --- max_tokens resolution: no self-imposed cap may fail or truncate a call ---
# Owner directive (2026-08-30): 0/None = the model's own maximum output; explicit
# values clamp DOWN to the model's max; a provider "max_tokens too large" 400 is
# retried once at the provider-stated limit instead of failing the turn.

def test_resolve_max_tokens_sentinel_uses_model_max(monkeypatch):
    monkeypatch.setattr(llm_compat, "model_max_output_tokens", lambda m: 32000)
    assert llm_compat.resolve_max_tokens(0, "anthropic/x") == 32000
    assert llm_compat.resolve_max_tokens(None, "anthropic/x") == 32000


def test_resolve_max_tokens_sentinel_falls_back_when_model_unknown(monkeypatch):
    monkeypatch.setattr(llm_compat, "model_max_output_tokens", lambda m: None)
    assert llm_compat.resolve_max_tokens(0, "mystery/model") == llm_compat.FALLBACK_MAX_OUTPUT_TOKENS


def test_resolve_max_tokens_explicit_value_clamped_to_model_max(monkeypatch):
    monkeypatch.setattr(llm_compat, "model_max_output_tokens", lambda m: 8192)
    assert llm_compat.resolve_max_tokens(999999, "small/model") == 8192
    # An explicit value UNDER the model max is honored (deliberate cost cap).
    assert llm_compat.resolve_max_tokens(4000, "small/model") == 4000


def test_sentinel_max_tokens_resolved_before_provider_call(monkeypatch):
    calls = []

    async def fake(**kwargs):
        calls.append(dict(kwargs))
        return "OK"

    monkeypatch.setattr(litellm, "acompletion", fake)
    monkeypatch.setattr(llm_compat, "model_max_output_tokens", lambda m: 32000)
    out = asyncio.run(llm_compat.acompletion(model="anthropic/x", max_tokens=0))
    assert out == "OK"
    assert calls[0]["max_tokens"] == 32000   # sentinel resolved, provider never sees 0


def test_max_tokens_too_large_retried_at_provider_stated_limit(monkeypatch):
    calls = []
    _TOO_LARGE = (
        "litellm.BadRequestError: AnthropicException - "
        '{"type":"error","error":{"type":"invalid_request_error",'
        '"message":"max_tokens: 64000 > 32000, which is the maximum allowed number '
        'of output tokens for this model"}}'
    )

    async def fake(**kwargs):
        calls.append(dict(kwargs))
        if kwargs["max_tokens"] > 32000:
            raise Exception(_TOO_LARGE)
        return "GENERATED"

    monkeypatch.setattr(litellm, "acompletion", fake)
    monkeypatch.setattr(llm_compat, "model_max_output_tokens", lambda m: None)  # unknown model
    out = asyncio.run(llm_compat.acompletion(model="mystery/new-model", max_tokens=0))
    assert out == "GENERATED"
    assert len(calls) == 2
    assert calls[0]["max_tokens"] == llm_compat.FALLBACK_MAX_OUTPUT_TOKENS
    assert calls[1]["max_tokens"] == 32000   # the limit the provider itself stated


def test_max_tokens_retry_happens_at_most_once(monkeypatch):
    calls = []

    async def fake(**kwargs):
        calls.append(dict(kwargs))
        raise Exception("max_tokens is too large: supports at most 123 output tokens")

    monkeypatch.setattr(litellm, "acompletion", fake)
    monkeypatch.setattr(llm_compat, "model_max_output_tokens", lambda m: None)
    with pytest.raises(Exception, match="too large"):
        asyncio.run(llm_compat.acompletion(model="x", max_tokens=50000))
    assert len(calls) == 2   # original + ONE retry, then re-raise


def test_max_tokens_limit_parser_ignores_unrelated_errors():
    assert llm_compat._max_tokens_limit_from_error("RateLimitError: 429 too many requests", 64000) is None
    assert llm_compat._max_tokens_limit_from_error("`temperature` is deprecated", 64000) is None
