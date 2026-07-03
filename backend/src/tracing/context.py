"""Request-scoped trace context.

Contextvars instead of parameters: the trace id has to reach the LLM gateway
and usage metering from any call depth without threading an argument through
every signature in between. Set by TraceContextMiddleware per HTTP request;
WebSocket flows (builder chat) can set it explicitly when they join the spine
in a later phase.
"""
from __future__ import annotations

import contextvars
import re

# SDK ids are crypto.randomUUID(); accept any header that is plausibly an id
# and reject anything that could be log-injection or absurdly long.
_TRACE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

current_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "aihub_trace_id", default=None
)

# Span id of the most recent instrumented LLM call in this context. Consumed
# (and cleared) by llm_usage.record_usage so a usage row joins to exactly the
# span it meters — never to a later call's span.
last_span_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "aihub_last_span_id", default=None
)


def parse_trace_id(raw: object) -> str | None:
    """The validated trace id, or None for absent/malformed values."""
    if isinstance(raw, str) and _TRACE_ID_RE.match(raw):
        return raw
    return None
