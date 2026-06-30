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
    """
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
