"""Decision execution + registry.

Invoke semantics: NEVER raises for LLM trouble. Timeout, provider error,
unparseable output, or schema violation all resolve to the declared fallback
(source="fallback") and an error-tagged span — apps stay deterministic when
the model misbehaves.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai_providers.service import ai_provider_service
from .models import AppDecision, DecisionCache

logger = logging.getLogger(__name__)

# Default per-invocation LLM budget; decisions can override (manifest/PUT,
# clamped to _TIMEOUT_MAX). 6s proved far too tight in real testing — a big
# model *generating* content (not just classifying) routinely needs 20-60s.
DEFAULT_TIMEOUT_SECONDS = 30
_TIMEOUT_MAX = 120
# Output ceiling per invocation. 1024 proved too small in real testing —
# generate-style decisions (multiple prompts/arrays) got truncated mid-JSON
# and burned their fallback on an "unparseable" answer.
DECISION_MAX_TOKENS = 4096
_INPUT_MAX_CHARS = 20_000
_NAME_MAX = 100

_TYPE_MAP = {
    "string": str, "number": (int, float), "integer": int,
    "boolean": bool, "object": dict, "array": list,
}


class DecisionError(Exception):
    """Registry-level problems (unknown decision, bad manifest) — real errors,
    unlike LLM trouble which resolves to the fallback."""


# ------------------------------------------------------------------ registry

def validate_manifest_entry(entry: dict) -> list[str]:
    errors = []
    name = entry.get("name")
    if not isinstance(name, str) or not name or len(name) > _NAME_MAX \
            or not all(c.isalnum() or c in "_-" for c in name):
        errors.append(f"invalid decision name {name!r} (alphanumeric/_/- up to {_NAME_MAX})")
    if not isinstance(entry.get("prompt"), str) or not entry["prompt"].strip():
        errors.append(f"decision {name!r}: prompt is required")
    if "fallback" not in entry:
        errors.append(f"decision {name!r}: fallback is required (deployed apps "
                      "depend on it when the platform or model is unreachable)")
    schema = entry.get("output_schema")
    if schema is not None and not isinstance(schema, dict):
        errors.append(f"decision {name!r}: output_schema must be an object")
    return errors


async def upsert_from_manifest(db: AsyncSession, app_id: str, entries: list[dict]) -> tuple[list[str], list[str]]:
    """Upsert declared decisions PER ENTRY: valid entries register, invalid
    ones are skipped and reported. Deliberately not all-or-nothing — one bad
    entry used to block the whole manifest, so the app shipped calling
    decisions that were never registered (registry drift → runtime 404s).
    Returns (names_written, entry_errors)."""
    written: list[str] = []
    errors: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            errors.append("manifest entry is not an object")
            continue
        entry_errors = validate_manifest_entry(entry)
        if entry_errors:
            errors.extend(entry_errors)
            continue
        row = (await db.execute(select(AppDecision).where(
            AppDecision.app_id == app_id, AppDecision.name == entry["name"],
        ))).scalar_one_or_none()
        creating = row is None
        if creating:
            row = AppDecision(app_id=app_id, name=entry["name"], prompt_template="", fallback_json="null")
            db.add(row)
        # The generator's manifest SEEDS a decision; once the row exists, the
        # admin-tunable knobs (prompt, model, temperature, cache TTL) are
        # platform data that a regeneration/self-heal re-save must NOT clobber.
        if not row.prompt_template:
            row.prompt_template = entry["prompt"]
        if creating:
            if entry.get("model"):
                row.model = str(entry["model"])[:100]
            if isinstance(entry.get("temperature"), (int, float)):
                row.temperature = float(entry["temperature"])
            if isinstance(entry.get("cache_ttl_seconds"), int):
                row.cache_ttl_seconds = max(0, entry["cache_ttl_seconds"])
            if isinstance(entry.get("timeout_seconds"), int):
                row.timeout_seconds = min(max(1, entry["timeout_seconds"]), _TIMEOUT_MAX)
        # Contract fields stay generator-owned — but never regress to nothing
        # when a later manifest omits the schema.
        row.description = entry.get("description", row.description or "")[:300]
        if entry.get("output_schema") is not None:
            row.output_schema_json = entry.get("output_schema")
        row.fallback_json = json.dumps(entry.get("fallback"))
        written.append(row.name)
    await db.commit()
    return written, errors


async def sync_from_draft(db: AsyncSession, app_id: str) -> tuple[list[str], list[str]]:
    """Read the draft's decisions.json from disk and upsert it into the
    registry. The generation hook only fires on turns that RE-EMIT the
    manifest — an app whose manifest landed under an older backend process
    (before the hook/tables existed) drifts silently and every aiDecide 404s.
    Called on every preview start so the registry self-heals."""
    import json as _json
    from pathlib import Path
    from ..config import settings

    base = Path(settings.app_data_dir) / app_id / "draft" / "frontend"
    for candidate in (base / "decisions.json", base / "src" / "decisions.json"):
        if candidate.exists():
            try:
                entries = _json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                return [], [f"decisions.json unreadable: {e}"]
            if not isinstance(entries, list):
                return [], ["decisions.json must be a JSON array"]
            return await upsert_from_manifest(db, app_id, entries)
    return [], []


async def purge_expired_cache(db: AsyncSession) -> int:
    """Delete expired cache rows platform-wide (called by the janitor loop;
    invoke() also purges per-decision opportunistically)."""
    result = await db.execute(delete(DecisionCache).where(
        DecisionCache.expires_at < datetime.now(timezone.utc)))
    await db.commit()
    return result.rowcount or 0


async def list_decisions(db: AsyncSession, app_id: str) -> list[AppDecision]:
    return list((await db.execute(
        select(AppDecision).where(AppDecision.app_id == app_id).order_by(AppDecision.name)
    )).scalars().all())


async def get_decision(db: AsyncSession, app_id: str, name: str) -> AppDecision | None:
    return (await db.execute(select(AppDecision).where(
        AppDecision.app_id == app_id, AppDecision.name == name,
    ))).scalar_one_or_none()


async def update_prompt(db: AsyncSession, decision: AppDecision, *, prompt: str | None,
                        model: str | None, temperature: float | None,
                        cache_ttl_seconds: int | None,
                        timeout_seconds: int | None = None) -> None:
    """Prompt-as-data: takes effect on the NEXT invocation, zero rebuild.
    Cache keys include the prompt hash, so edits invalidate implicitly."""
    if prompt is not None and prompt.strip():
        decision.prompt_template = prompt
    if model is not None:
        decision.model = model.strip() or None
    if temperature is not None:
        decision.temperature = temperature
    if cache_ttl_seconds is not None:
        decision.cache_ttl_seconds = max(0, cache_ttl_seconds)
    if timeout_seconds is not None:
        decision.timeout_seconds = min(max(1, timeout_seconds), _TIMEOUT_MAX)
    await db.commit()


# ------------------------------------------------------------------- invoke

def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _cache_key(decision: AppDecision, input_obj: dict, user_scope: str) -> str:
    material = "|".join([
        hashlib.sha256(decision.prompt_template.encode()).hexdigest(),
        decision.model or "", str(decision.temperature),
        user_scope, _canonical(input_obj),
    ])
    return hashlib.sha256(material.encode()).hexdigest()


def _parse_output(raw: str, schema: dict | None):
    """(value, ok). Tolerant: fences stripped, first JSON block extracted;
    bare text is accepted only when the schema asks for a string."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = "\n".join(l for l in text.splitlines() if not l.strip().startswith("```")).strip()
    for candidate in (text,):
        try:
            return json.loads(candidate), True
        except json.JSONDecodeError:
            pass
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start >= 0:
        end = max(text.rfind("}"), text.rfind("]"))
        if end > start:
            try:
                return json.loads(text[start:end + 1]), True
            except json.JSONDecodeError:
                pass
    # Cheap/fast models often answer a bare enum member without JSON quoting
    # despite the schema note — accept it rather than burning the fallback.
    if schema and "enum" in schema and text:
        bare = text.strip().strip('"\'')
        if bare in schema["enum"]:
            return bare, True
    if schema and schema.get("type") == "string" and text:
        return text.strip('"'), True
    return None, False


def _validate_output(value, schema: dict | None) -> bool:
    """Minimal, deliberate: enum membership + top-level type. Full JSON-Schema
    validation is not worth a dependency until evals land (Phase 3+)."""
    if not schema:
        return True
    if "enum" in schema:
        return value in schema["enum"]
    expected = _TYPE_MAP.get(schema.get("type", ""))
    if expected is not None:
        if expected is int and isinstance(value, bool):
            return False
        return isinstance(value, expected)
    return True


async def invoke(db: AsyncSession, decision: AppDecision, input_obj: dict,
                 user_id: str | None) -> dict:
    """Execute one decision. Returns {value, source, latency_ms}."""
    t0 = time.monotonic()
    fallback = json.loads(decision.fallback_json)
    user_scope = user_id or "(anon)"
    decision_span_id = str(uuid.uuid4())

    def _result(value, source, *, status="ok", error=None):
        latency_ms = int((time.monotonic() - t0) * 1000)
        _emit_decision_span(decision, decision_span_id, input_obj, value, source,
                            status=status, error=error, latency_ms=latency_ms,
                            user_id=user_id)
        return {"value": value, "source": source, "latency_ms": latency_ms}

    input_json = _canonical(input_obj)
    if len(input_json) > _INPUT_MAX_CHARS:
        return _result(fallback, "fallback", status="error",
                       error=f"input too large ({len(input_json)} chars)")

    # 1. Exact-match cache (user-scoped; prompt hash in the key).
    key = None
    if decision.cache_ttl_seconds > 0:
        key = _cache_key(decision, input_obj, user_scope)
        hit = (await db.execute(select(DecisionCache).where(
            DecisionCache.decision_id == decision.id, DecisionCache.cache_key == key,
        ))).scalar_one_or_none()
        if hit is not None:
            # SQLite round-trips datetimes offset-naive; normalize to UTC-aware.
            expires = hit.expires_at if hit.expires_at.tzinfo \
                else hit.expires_at.replace(tzinfo=timezone.utc)
            if expires > datetime.now(timezone.utc):
                return _result(json.loads(hit.value_json), "cache")

    # 2. Provider via the purpose pin (inherits generation default when unset).
    provider_config = await ai_provider_service.get_default_provider_config(db, purpose="decision")
    if not provider_config:
        return _result(fallback, "fallback", status="error", error="no AI provider configured")
    provider_type = provider_config["provider_type"]
    model = decision.model or provider_config["model"]
    llm_model = model if provider_type == "openai" else f"{provider_type}/{model}"

    schema_note = ""
    if decision.output_schema_json:
        schema_note = ("\n\nRespond with ONLY a JSON value matching this JSON Schema "
                       f"(no prose, no code fences):\n{json.dumps(decision.output_schema_json)}")
    messages = [
        {"role": "system", "content": decision.prompt_template + schema_note},
        {"role": "user", "content": input_json},
    ]

    # 3. The call — bounded, instrumented (the gateway emits the child ai.call
    #    span parented to this decision's span), metered.
    from ..llm_compat import acompletion
    try:
        response = await asyncio.wait_for(acompletion(
            model=llm_model,
            messages=messages,
            api_key=provider_config["api_key"],
            base_url=provider_config.get("base_url"),
            max_tokens=DECISION_MAX_TOKENS,
            temperature=decision.temperature,
            aihub_span={"app_id": decision.app_id, "user_id": user_id,
                        "purpose": "decision", "name": decision.name,
                        "parent_span_id": decision_span_id,
                        "provider_type": provider_type, "model": model},
        ), timeout=float(decision.timeout_seconds or DEFAULT_TIMEOUT_SECONDS))
        raw = response.choices[0].message.content or ""
    except asyncio.TimeoutError:
        # Self-documenting: the trace (and any AI reading it) must see WHICH
        # knob was hit and how to change it — not a bare "TimeoutError".
        budget = decision.timeout_seconds or DEFAULT_TIMEOUT_SECONDS
        msg = (f"timed out after {budget}s (this decision's timeout_seconds; "
               f"raise it in decisions.json or via PUT /api/decisions/"
               f"{decision.app_id}/{decision.name}, max {_TIMEOUT_MAX}s — "
               f"or pin a faster model to the 'App decisions' purpose)")
        await _meter(db, decision, user_id, provider_type, model, error=msg)
        return _result(fallback, "fallback", status="error", error=msg)
    except Exception as e:
        await _meter(db, decision, user_id, provider_type, model,
                     error=f"{type(e).__name__}: {e}")
        return _result(fallback, "fallback", status="error",
                       error=f"{type(e).__name__}: {str(e)[:300]}")

    await _meter(db, decision, user_id, provider_type, model, response=response, raw=raw)

    value, ok = _parse_output(raw, decision.output_schema_json)
    if not ok:
        # Self-documenting: a truncated answer is a sizing problem, not a
        # model-quality problem — say which and what to change.
        finish = getattr(response.choices[0], "finish_reason", None)
        if finish == "length":
            return _result(fallback, "fallback", status="error",
                           error=f"model output truncated at {DECISION_MAX_TOKENS} tokens "
                                 f"mid-JSON — this decision asks for too much output; "
                                 f"reduce the requested volume in its prompt")
        return _result(fallback, "fallback", status="error",
                       error=f"unparseable model output: {raw[:200]!r}")
    if not _validate_output(value, decision.output_schema_json):
        return _result(fallback, "fallback", status="error",
                       error=f"output failed schema check: {json.dumps(value)[:200]}")

    # 4. Cache store (+ opportunistic purge of this decision's expired rows —
    #    keeps the table bounded without a dedicated janitor).
    if key is not None:
        try:
            await db.execute(delete(DecisionCache).where(
                DecisionCache.decision_id == decision.id,
                DecisionCache.expires_at < datetime.now(timezone.utc),
            ))
            await db.execute(delete(DecisionCache).where(
                DecisionCache.decision_id == decision.id, DecisionCache.cache_key == key))
            db.add(DecisionCache(
                decision_id=decision.id, cache_key=key, value_json=json.dumps(value),
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=decision.cache_ttl_seconds),
            ))
            await db.commit()
        except Exception:
            try:
                await db.rollback()
            except Exception:
                pass

    return _result(value, "llm")


async def _meter(db, decision, user_id, provider_type, model, *,
                 response=None, raw="", error=None):
    """Best-effort llm_usage row (joins the gateway's ai.call span via the
    trace context). Rollback on failure — request-scoped session."""
    try:
        from ..llm_usage.service import record_usage
        usage = getattr(response, "usage", None)
        await record_usage(
            db, user_id=user_id or "(unknown)", app_id=decision.app_id,
            provider_type=provider_type or "", model=model or "",
            purpose="decision",
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or (max(1, len(raw) // 4) if raw else 0),
            error=error,
        )
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass


def _emit_decision_span(decision: AppDecision, span_id: str, input_obj, value,
                        source: str, *, status: str, error: str | None,
                        latency_ms: int, user_id: str | None) -> None:
    """The ai.decision span — the unit users reason about in story mode.
    Input/outcome ride the payload slots (capture level + Fernet apply)."""
    try:
        from ..tracing.context import current_trace_id
        from ..tracing.writer import span_writer
        span_writer.enqueue({
            "id": span_id,
            "trace_id": current_trace_id.get(),
            "app_id": decision.app_id,
            "user_id": user_id,
            "kind": "ai.decision",
            "purpose": "decision",
            "name": decision.name,
            "status": status,
            "error": error,
            "prompt_text": _canonical(input_obj),
            "response_text": json.dumps({"value": value, "source": source}),
            "latency_ms": latency_ms,
        })
    except Exception:
        logger.exception("decision span emission failed (ignored)")
