"""Compatibility shim over ``litellm.acompletion``.

Why this exists
---------------
Newer Anthropic Claude models (and OpenAI's o-series) have **deprecated the
``temperature`` parameter** and reject any request that sets it::

    litellm.BadRequestError: AnthropicException -
    {"type":"invalid_request_error","message":"`temperature` is deprecated for this model."}

Every LLM call in AIHub used to hard-code a ``temperature`` (0.7 for generation,
0.2 for fixes, ...), so picking one of those models broke app generation, the
bug self-heal loop, and the AI Toggle.

This wrapper makes those calls resilient:

1. It enables ``litellm.drop_params`` so litellm strips parameters it *knows* a
   given model doesn't accept (handles models already in litellm's model map).
2. As a backstop for brand-new models litellm doesn't recognise yet, it catches
   a provider "param not accepted" 400 and **retries once without that param**
   instead of failing the whole turn.

All app-facing LLM calls should go through :func:`acompletion` rather than
calling ``litellm.acompletion`` directly.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Substrings that mark a provider rejecting a parameter (case-insensitive).
_REJECTION_HINTS = (
    "deprecated",
    "unsupported",
    "not supported",
    "does not support",
    "no longer supported",
    "not allowed",
)

# Params we'll strip-and-retry when the provider rejects them. Ordered: we only
# pop the one the error actually names, so a model that drops several still
# converges over successive retries.
_DROPPABLE_PARAMS = ("temperature", "top_p", "presence_penalty", "frequency_penalty")


def _names_rejected_param(message: str, param: str) -> bool:
    m = message.lower()
    return param in m and any(hint in m for hint in _REJECTION_HINTS)


async def acompletion(**kwargs):
    """``litellm.acompletion`` that survives provider parameter deprecations.

    Accepts the exact same kwargs as ``litellm.acompletion`` (streaming or not)
    and returns its result unchanged. On a "param is deprecated/unsupported"
    400, it removes the offending sampling param and retries — up to a few times
    so a model that rejects several still succeeds.

    Tracing: pass ``aihub_span={"app_id", "user_id", "purpose", "name",
    "provider_type", "model"}`` (stripped before litellm sees it) and the call
    emits an ai.call span — best-effort, enqueued to the async span writer, so
    it can never add latency to or break the call it describes. Streaming
    calls are not yet instrumented (the generation path keeps its own
    generation_trace until the Phase 2 streaming wrapper).
    """
    span_meta = kwargs.pop("aihub_span", None)
    if span_meta is None or kwargs.get("stream"):
        return await _acompletion_raw(kwargs)

    import asyncio
    import time
    t0 = time.monotonic()
    try:
        response = await _acompletion_raw(kwargs)
    except asyncio.CancelledError:
        # A caller-imposed timeout (asyncio.wait_for) cancels us mid-flight;
        # CancelledError is a BaseException, so without this clause the child
        # ai.call span would silently vanish for the most common failure mode.
        _emit_span(span_meta, kwargs, latency_ms=int((time.monotonic() - t0) * 1000),
                   status="error", error="cancelled (caller timeout)")
        raise
    except Exception as e:
        _emit_span(span_meta, kwargs, latency_ms=int((time.monotonic() - t0) * 1000),
                   status="error", error=f"{type(e).__name__}: {e}")
        raise
    _emit_span(span_meta, kwargs, latency_ms=int((time.monotonic() - t0) * 1000),
               response=response)
    return response


async def _acompletion_raw(kwargs: dict):
    """The original strip-and-retry loop (mutates kwargs on retries)."""
    import litellm

    # Let litellm proactively drop params known-unsupported for the chosen model.
    litellm.drop_params = True

    attempts = 0
    while True:
        try:
            return await litellm.acompletion(**kwargs)
        except Exception as e:  # noqa: BLE001 - we re-raise anything we can't handle
            message = str(e)
            dropped = None
            for param in _DROPPABLE_PARAMS:
                if param in kwargs and _names_rejected_param(message, param):
                    dropped = param
                    break
            if dropped is None or attempts >= len(_DROPPABLE_PARAMS):
                raise
            attempts += 1
            kwargs.pop(dropped, None)
            logger.warning(
                "Model %s rejected '%s'; retrying without it (attempt %d).",
                kwargs.get("model"), dropped, attempts,
            )


def _emit_span(span_meta: dict, kwargs: dict, *, latency_ms: int,
               response=None, status: str = "ok", error: str | None = None) -> None:
    """Build and enqueue an ai.call span. Never raises."""
    try:
        import json
        import uuid

        from .llm_usage.service import estimate_cost_usd
        from .tracing.context import current_trace_id, last_span_id
        from .tracing.writer import span_writer

        provider_type = span_meta.get("provider_type") or ""
        model = span_meta.get("model") or kwargs.get("model") or ""
        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "prompt_tokens", 0) or 0
        output_tokens = getattr(usage, "completion_tokens", 0) or 0

        response_text = None
        if response is not None:
            try:
                response_text = response.choices[0].message.content
            except Exception:
                response_text = None

        span_id = str(uuid.uuid4())
        span_writer.enqueue({
            "id": span_id,
            "trace_id": span_meta.get("trace_id") or current_trace_id.get(),
            "parent_span_id": span_meta.get("parent_span_id"),
            "app_id": span_meta.get("app_id") or "",
            "user_id": span_meta.get("user_id"),
            "kind": "ai.call",
            "purpose": span_meta.get("purpose") or "",
            "name": span_meta.get("name"),
            "provider_type": provider_type,
            "model": model,
            "status": status,
            "error": error,
            "prompt_text": json.dumps(kwargs.get("messages") or [], ensure_ascii=False),
            "response_text": response_text,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": estimate_cost_usd(provider_type, model, input_tokens, output_tokens),
            "latency_ms": latency_ms,
        })
        # Consumed (and cleared) by the next record_usage in this context.
        last_span_id.set(span_id)
    except Exception:
        logger.exception("span emission failed (ignored)")
