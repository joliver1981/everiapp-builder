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
