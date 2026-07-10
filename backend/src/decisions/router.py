"""Decision APIs.

POST /{app_id}/{name}/invoke — called by the SDK's aiDecide(). Auth matches
the AI Toggle (flexible bearer/cookie, 401 anonymous): every invocation costs
real tokens, so it stays attributable.
GET  /{app_id} + PUT /{app_id}/{name} — the registry (prompt-as-data: edits
apply on the next invocation, no rebuild).
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user_flexible
from ..auth.models import User
from ..database import get_db
from ..apps.service import apps_service
from . import service as decisions_service

router = APIRouter()


class InvokeRequest(BaseModel):
    input: dict = Field(default_factory=dict)


class DecisionUpdate(BaseModel):
    prompt: str | None = None
    model: str | None = None
    temperature: float | None = Field(None, ge=0.0, le=2.0)
    cache_ttl_seconds: int | None = Field(None, ge=0, le=30 * 86400)
    timeout_seconds: int | None = Field(None, ge=1, le=120)
    # Bounds mirror service._MAX_TOKENS_MIN/_MAX (a cap, not a target).
    max_output_tokens: int | None = Field(None, ge=16, le=64000)


def _to_response(d) -> dict:
    import json
    return {
        "id": d.id, "app_id": d.app_id, "name": d.name, "description": d.description,
        "prompt_template": d.prompt_template,
        "output_schema": d.output_schema_json,
        "fallback": json.loads(d.fallback_json),
        "model": d.model, "temperature": d.temperature,
        "cache_ttl_seconds": d.cache_ttl_seconds,
        "timeout_seconds": d.timeout_seconds,
        "max_output_tokens": d.max_output_tokens,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
    }


@router.post("/{app_id}/{name}/invoke")
async def invoke_decision(
    app_id: str,
    name: str,
    body: InvokeRequest,
    user: User = Depends(get_current_user_flexible),
    db: AsyncSession = Depends(get_db),
):
    # Every invoke is an LLM completion — same token-bucket discipline as the
    # other resource-costing runtime endpoints (datasets, app-DB, chat).
    from ..rate_limit import decision_limiter
    if not decision_limiter.allow(f"{user.id}:{app_id}"):
        raise HTTPException(status_code=429, detail="Decision rate limit exceeded — slow down")
    app = await apps_service.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="App not found")
    decision = await decisions_service.get_decision(db, app_id, name)
    if not decision:
        raise HTTPException(status_code=404, detail=f"Unknown decision '{name}'")
    return await decisions_service.invoke(db, decision, body.input, user.id)


@router.post("/{app_id}/sync")
async def sync_decisions(
    app_id: str,
    user: User = Depends(get_current_user_flexible),
    db: AsyncSession = Depends(get_db),
):
    """Re-sync the registry from the draft's decisions.json — the manual
    escape hatch for registry drift (also runs automatically on every
    preview start)."""
    if user.role not in ("admin", "developer"):
        raise HTTPException(status_code=403, detail="Developer or admin role required")
    app = await apps_service.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="App not found")
    written, errors = await decisions_service.sync_from_draft(db, app_id)
    return {"registered": written, "errors": errors}


@router.get("/{app_id}")
async def list_decisions(
    app_id: str,
    user: User = Depends(get_current_user_flexible),
    db: AsyncSession = Depends(get_db),
):
    app = await apps_service.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="App not found")
    return [_to_response(d) for d in await decisions_service.list_decisions(db, app_id)]


@router.put("/{app_id}/{name}")
async def update_decision(
    app_id: str,
    name: str,
    body: DecisionUpdate,
    user: User = Depends(get_current_user_flexible),
    db: AsyncSession = Depends(get_db),
):
    if user.role not in ("admin", "developer"):
        raise HTTPException(status_code=403, detail="Developer or admin role required")
    decision = await decisions_service.get_decision(db, app_id, name)
    if not decision:
        raise HTTPException(status_code=404, detail=f"Unknown decision '{name}'")
    await decisions_service.update_prompt(
        db, decision, prompt=body.prompt, model=body.model,
        temperature=body.temperature, cache_ttl_seconds=body.cache_ttl_seconds,
        timeout_seconds=body.timeout_seconds,
        max_output_tokens=body.max_output_tokens,
    )
    return _to_response(decision)
