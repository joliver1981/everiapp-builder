"""LLM stream watchdog + actionable network errors (the 'Thinking... forever' class).

A stalled provider connection used to hang the builder turn indefinitely with
no feedback: no timeout on the litellm call, and the chat loop simply awaited
the next chunk forever. Field case: an installed server whose DNS resolved
api.openai.com but not api.anthropic.com — OpenAI built apps fine, Anthropic
froze on "Thinking...". Locked-in behaviors:

  - _stream_with_watchdog relays chunks, emits heartbeat statuses during
    silence, and FAILS the turn with an actionable error at the silence cap;
  - stream errors from the provider propagate (not swallowed);
  - _actionable_llm_error appends the operator playbook (DNS/firewall/proxy)
    to provider-unreachable errors and leaves other errors untouched;
  - llm_compat.acompletion defaults a finite timeout onto every call.
"""
import asyncio

import pytest

from src.ai.service import _actionable_llm_error, _stream_with_watchdog


async def _collect(gen):
    out = []
    async for item in gen:
        out.append(item)
    return out


def test_watchdog_relays_a_lively_stream_untouched():
    async def stream():
        for i in range(3):
            yield f"chunk-{i}"

    events = asyncio.run(_collect(_stream_with_watchdog(
        stream(), "anthropic/claude-fable-5", silence_timeout=5.0)))
    assert events == [("chunk", "chunk-0"), ("chunk", "chunk-1"), ("chunk", "chunk-2")]


def test_watchdog_heartbeats_then_fails_on_total_silence():
    async def silent_stream():
        await asyncio.sleep(60)  # never yields within the test's timeout
        yield "never"

    async def run():
        events = []
        gen = _stream_with_watchdog(
            silent_stream(), "anthropic/claude-fable-5",
            silence_timeout=0.3, status_interval=0.1)
        with pytest.raises(RuntimeError) as exc:
            async for item in gen:
                events.append(item)
        return events, str(exc.value)

    events, err = asyncio.run(run())
    assert events, "expected heartbeat status events before the failure"
    assert all(kind == "status" for kind, _ in events)
    assert "Still waiting on anthropic/claude-fable-5" in events[0][1]
    assert "No response data from anthropic/claude-fable-5" in err


def test_watchdog_silence_counter_resets_on_activity():
    async def slow_but_alive():
        for i in range(3):
            await asyncio.sleep(0.15)  # below the cap, above the status interval
            yield f"chunk-{i}"

    events = asyncio.run(_collect(_stream_with_watchdog(
        slow_but_alive(), "p/m", silence_timeout=0.4, status_interval=0.1)))
    chunks = [p for k, p in events if k == "chunk"]
    assert chunks == ["chunk-0", "chunk-1", "chunk-2"], \
        "steady sub-cap gaps must never abort the stream"


def test_watchdog_propagates_stream_errors():
    async def broken_stream():
        yield "first"
        raise ValueError("provider exploded mid-stream")

    async def run():
        events = []
        with pytest.raises(ValueError, match="provider exploded"):
            async for item in _stream_with_watchdog(
                    broken_stream(), "p/m", silence_timeout=5.0):
                events.append(item)
        return events

    events = asyncio.run(run())
    assert events == [("chunk", "first")]


# ---- actionable error mapping ------------------------------------------------

def test_network_errors_get_the_operator_playbook():
    raw = ("litellm.InternalServerError: AnthropicException - Cannot connect to host "
           "api.anthropic.com:443 ssl:<ssl.SSLContext> [getaddrinfo failed]")
    msg = _actionable_llm_error("anthropic", "claude-fable-5", raw)
    assert "could not reach the anthropic API" in msg
    assert "DNS" in msg and "HTTPS_PROXY" in msg
    assert "getaddrinfo failed" in msg  # raw detail preserved


def test_non_network_errors_pass_through_unchanged():
    raw = "BadRequestError: max_tokens must be positive"
    assert _actionable_llm_error("openai", "gpt-5.5", raw) == raw


# ---- llm_compat default timeouts --------------------------------------------

def test_acompletion_defaults_a_finite_timeout(monkeypatch):
    import litellm

    from src import llm_compat
    from src.config import settings

    captured: list[dict] = []

    async def fake_acompletion(**kwargs):
        captured.append(dict(kwargs))
        return "ok"

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    asyncio.run(llm_compat.acompletion(model="anthropic/x", stream=True, messages=[]))
    asyncio.run(llm_compat.acompletion(model="anthropic/x", messages=[]))
    asyncio.run(llm_compat.acompletion(model="anthropic/x", messages=[], timeout=7))

    assert captured[0]["timeout"] == settings.llm_stream_timeout
    assert captured[1]["timeout"] == settings.llm_request_timeout
    assert captured[2]["timeout"] == 7, "explicit caller timeout must win"
