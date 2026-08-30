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


# --- max_tokens resolution: "the provider's limit is the only limit" ---------
# Platform policy (owner directive 2026-08-30): output caps must never be the
# reason a call fails or truncates. max_tokens=0/None means "no platform cap" —
# resolve to the MODEL's maximum output. A positive value (explicit admin cost
# cap) is still clamped down to the model's max so it can never 400.

# Used when litellm's registry doesn't know the model (brand-new or free-form
# ids). Deliberately generous — a cap is never a target — and the too-large
# retry below recovers the exact limit if the provider objects.
FALLBACK_MAX_OUTPUT_TOKENS = 64000

_MAX_TOKENS_ERROR_HINTS = (
    "max_tokens", "max tokens", "max_completion_tokens", "output tokens",
)


def model_max_output_tokens(model: str | None) -> int | None:
    """Best-effort lookup of a model's maximum output tokens via litellm's
    registry. None when the model is unknown (never raises)."""
    if not model:
        return None
    try:
        import litellm
        info = litellm.get_model_info(model)
        val = (info or {}).get("max_output_tokens") or (info or {}).get("max_tokens")
        return int(val) if val else None
    except Exception:
        return None


def resolve_max_tokens(requested: int | None, model: str | None) -> int:
    """Effective max_tokens for a call: 0/None = the model's own maximum
    (fallback when unknown); a positive request is clamped to the model's max."""
    limit = model_max_output_tokens(model)
    if not requested or requested <= 0:
        return limit or FALLBACK_MAX_OUTPUT_TOKENS
    if limit and requested > limit:
        return limit
    return int(requested)


def _max_tokens_limit_from_error(message: str, requested: int) -> int | None:
    """If `message` is a provider rejecting max_tokens as too large, extract the
    limit it states (e.g. Anthropic: "max_tokens: 128000 > 64000, which is the
    maximum..."; OpenAI: "This model supports at most 16384 completion tokens").
    Returns a safe retry value, or None when this isn't that error."""
    low = message.lower()
    if not any(h in low for h in _MAX_TOKENS_ERROR_HINTS):
        return None
    import re
    candidates = [int(n) for n in re.findall(r"\d{2,}", message)]
    stated = [n for n in candidates if 16 <= n < requested]
    if stated:
        return max(stated)
    # Provider named max_tokens but no parseable limit — only worth one blind
    # retry if the request was actually large.
    return 16384 if requested > 16384 else None


def litellm_model(provider_type: str | None, model: str | None) -> str:
    """Compose the litellm model string from a provider's (type, model) pair.

    ALWAYS provider-prefixed ("openai/gpt-5.5", "anthropic/claude-...").
    We used to pass OpenAI models BARE and let litellm infer the provider from
    its built-in model registry — which breaks for every OpenAI model newer
    than the pinned litellm release ("LLM Provider NOT provided", hit live on
    a fresh install with our own preset defaults gpt-5.5/gpt-5.4, while
    Anthropic worked because it was prefixed). An explicit prefix skips
    registry inference entirely, so brand-new model ids just work, and it is
    also litellm's documented route for OpenAI-compatible base_url servers.

    If the admin already typed the prefix ("openai/gpt-5.4"), it is not
    doubled. Provider-NATIVE ids that themselves contain "/" (OpenRouter's
    "anthropic/claude-3-opus") still get the provider prefix — only an exact
    "<provider_type>/" prefix counts as already-routed.
    """
    m = (model or "").strip()
    p = (provider_type or "").strip()
    if not p or not m:
        return m
    if m.startswith(p + "/"):
        return m
    return f"{p}/{m}"


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

    # Never let a provider call wait forever (callers may override). For
    # streaming, the HTTP client applies this per read — an inter-chunk gap
    # cap, generous enough for long time-to-first-token on huge prompts; for
    # non-streaming it caps the whole request.
    from .config import settings
    kwargs.setdefault(
        "timeout",
        settings.llm_stream_timeout if kwargs.get("stream") else settings.llm_request_timeout,
    )

    # No platform cap may cause a failure or silent truncation: 0/None/absent
    # max_tokens becomes the model's maximum output; explicit values are clamped
    # to the model's max when litellm knows it. (Owner directive — see
    # resolve_max_tokens.)
    kwargs["max_tokens"] = resolve_max_tokens(kwargs.get("max_tokens"), kwargs.get("model"))

    attempts = 0
    max_tokens_retried = False
    while True:
        try:
            return await litellm.acompletion(**kwargs)
        except Exception as e:  # noqa: BLE001 - we re-raise anything we can't handle
            message = str(e)
            # A model unknown to litellm's registry can still reject our
            # resolved max_tokens as too large — retry ONCE at the limit the
            # provider itself states, so maxing out never fails a turn.
            if not max_tokens_retried:
                stated = _max_tokens_limit_from_error(message, int(kwargs.get("max_tokens") or 0))
                if stated:
                    max_tokens_retried = True
                    logger.warning(
                        "Model %s rejected max_tokens=%s; retrying at provider-stated limit %d.",
                        kwargs.get("model"), kwargs.get("max_tokens"), stated,
                    )
                    kwargs["max_tokens"] = stated
                    continue
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
