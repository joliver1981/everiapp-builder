"""Copilot APIs — D2: diagnose a traced issue on demand.

Reuses the bug-report analyzer end to end (source collection with its size
budgets, the registry-resolved system prompt, tolerant parsing) — one
diagnosis pipeline in the platform, not two. Suggest-level only: the result
is shown to the developer; nothing is written to disk.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user
from ..auth.models import User
from ..database import get_db
from ..apps.service import apps_service

router = APIRouter()


class DiagnoseRequest(BaseModel):
    issue_label: str = Field(..., min_length=1, max_length=300)
    trace_id: str | None = Field(None, max_length=64)
    # The Inspector's current span window (client-shaped dicts, capped like
    # the bug-report intake).
    spans: list[dict] = Field(default_factory=list, max_length=100)


@router.post("/{app_id}/diagnose")
async def diagnose(
    app_id: str,
    body: DiagnoseRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if user.role not in ("admin", "developer"):
        raise HTTPException(status_code=403, detail="Developer or admin role required")

    from ..rate_limit import copilot_limiter
    if not copilot_limiter.allow(user.id):
        raise HTTPException(status_code=429, detail="Copilot rate limit exceeded — slow down")

    app = await apps_service.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="App not found")

    # Same monthly-budget gate as generation: a diagnosis is a real LLM call.
    from ..platform_settings.service import check_budget
    budget = await check_budget(db, user.id)
    if not budget.allowed:
        raise HTTPException(status_code=402, detail=f"LLM budget exceeded: {budget.reason}")

    from ..bug_reports.analyzer import run_analysis
    result = await run_analysis(
        db,
        app_id=app_id,
        version=None,  # diagnose the DRAFT — that's what the builder previews
        bug_title=body.issue_label,
        bug_description="Issue detected by the trace Inspector while testing the app preview. "
                        "Diagnose the root cause from the traced events and source.",
        captured_context={"trace_id": body.trace_id, "recent_spans": body.spans},
        usage_purpose="copilot_diagnose",
        usage_user=user.id,
    )
    if result.error:
        raise HTTPException(status_code=502, detail=result.error)
    return {
        "diagnosis": result.diagnosis,
        "root_cause": result.root_cause,
        "risk_level": result.risk_level,
        "risk_rationale": result.risk_rationale,
        # Suggest level: name the implicated files — applying a fix stays a
        # human action (Co-fix lands behind the fix mutex later).
        "files_implicated": [
            {"path": f.get("path", ""), "action": f.get("action", "update")}
            for f in (result.proposed_files or []) if isinstance(f, dict)
        ],
        "llm_model": result.llm_model,
    }
