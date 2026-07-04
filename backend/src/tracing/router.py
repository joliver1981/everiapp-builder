"""Span APIs.

GET  /{app_id}/spans — metadata only until the viewer adds an audited
payload-decrypt path.
POST /{app_id}/spans — batched client-side spans from the SDK (dataset/app-DB
calls, UI errors/interactions). Auth-optional like the bug-report intake:
deployed apps may have no platform token; a valid bearer adds attribution.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user
from ..auth.models import User
from ..database import get_db
from ..apps.service import apps_service
from .context import parse_trace_id
from .service import list_spans

router = APIRouter()

# Client spans carry a small JSON detail (query params, element label, error
# stack) — stored via the writer's payload slot, so capture level + encryption
# apply exactly as they do to LLM prompts.
CLIENT_SPAN_KINDS = {"dataset.query", "appdb.call", "http.call", "ui.error", "ui.interaction"}
_DETAIL_MAX_CHARS = 10_000


class ClientSpan(BaseModel):
    kind: str
    name: str = Field("", max_length=100)
    trace_id: str | None = Field(None, max_length=64)
    parent_span_id: str | None = Field(None, max_length=36)
    status: str = Field("ok", pattern="^(ok|error)$")
    error: str | None = Field(None, max_length=500)
    latency_ms: int = Field(0, ge=0, le=3_600_000)
    detail: str | None = None  # JSON string, truncated server-side


class ClientSpanBatch(BaseModel):
    spans: list[ClientSpan] = Field(..., min_length=1, max_length=100)


# Per-app spans/minute budget for the ANONYMOUS path. In-memory (single-node
# on-prem); a browser session emits a few spans per user action, so this is
# generous for real apps and cheap insurance against a flood. Authenticated
# callers are not throttled here (they're accountable via their token).
_ANON_SPANS_PER_MINUTE = 2000
_anon_windows: dict[str, tuple[int, int]] = {}  # app_id -> (minute_bucket, count)


def _anon_over_budget(app_id: str, n: int) -> bool:
    import time
    bucket = int(time.time() // 60)
    prev_bucket, count = _anon_windows.get(app_id, (bucket, 0))
    if prev_bucket != bucket:
        count = 0
    count += n
    _anon_windows[app_id] = (bucket, count)
    if len(_anon_windows) > 5000:  # bounded memory; stale apps age out
        _anon_windows.clear()
    return count > _ANON_SPANS_PER_MINUTE


@router.post("/{app_id}/spans", status_code=202)
async def ingest_spans(
    app_id: str,
    body: ClientSpanBatch,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    app = await apps_service.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="App not found")

    # Optional attribution — no 401 (deployed apps can be tokenless).
    user_id = None
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        from ..auth.service import auth_service
        payload = auth_service.decode_access_token(auth_header[7:])
        if payload:
            user_id = payload.get("sub")

    if user_id is None:
        # Anonymous writes need the same per-app opt-in as anonymous bug
        # reports: the developer flips the widget flag before the app becomes
        # an unauthenticated write endpoint.
        if not app.bug_widget_enabled:
            raise HTTPException(
                status_code=403,
                detail="Anonymous tracing requires the bug/telemetry widget to be enabled for this app",
            )
        if _anon_over_budget(app_id, len(body.spans)):
            raise HTTPException(status_code=429, detail="Span budget exceeded for this app")

    from .writer import span_writer
    accepted = 0
    for s in body.spans:
        if s.kind not in CLIENT_SPAN_KINDS:
            continue  # unknown kinds are skipped, not fatal — SDKs may be newer
        span_writer.enqueue({
            "id": str(uuid.uuid4()),
            "trace_id": parse_trace_id(s.trace_id),
            "parent_span_id": s.parent_span_id,
            "app_id": app_id,  # always the path — clients can't spoof another app
            "user_id": user_id,
            "kind": s.kind,
            "purpose": "app_runtime",
            "name": s.name or None,
            "status": s.status,
            "error": s.error,
            "prompt_text": (s.detail or "")[:_DETAIL_MAX_CHARS] or None,
            "latency_ms": s.latency_ms,
        })
        accepted += 1
    return {"accepted": accepted}


@router.delete("/{app_id}/spans")
async def clear_app_spans(
    app_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Clear the app's trace — a fresh slate before a test session."""
    app = await apps_service.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="App not found")
    if user.role not in ("admin", "developer"):
        raise HTTPException(status_code=403, detail="Developer or admin role required")
    from sqlalchemy import delete as sql_delete
    from .models import AISpan
    result = await db.execute(sql_delete(AISpan).where(AISpan.app_id == app_id))
    await db.commit()
    return {"deleted": result.rowcount or 0}


@router.get("/{app_id}/spans")
async def get_app_spans(
    app_id: str,
    limit: int = Query(200, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    app = await apps_service.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="App not found")
    # Same audience as the builder: the app's developers and admins.
    if user.role not in ("admin", "developer"):
        raise HTTPException(status_code=403, detail="Developer or admin role required")
    return await list_spans(db, app_id, limit=limit)
