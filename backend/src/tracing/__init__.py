"""Unified app tracing — Phase 1: the trace spine.

The SDK stamps every request with X-AIHub-Trace-Id (one id per app session);
TraceContextMiddleware parses it into a contextvar; the llm_compat gateway
emits an ai.call span per (non-streaming) LLM round-trip; llm_usage rows carry
trace_id/span_id so cost joins to spans. Capture level and retention are
platform settings applied at write time by the async span writer.
"""
