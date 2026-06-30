from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..apps.models import App
from ..auth.dependencies import get_current_user_flexible, require_role
from ..auth.models import User
from ..auth.service import auth_service
from ..database import get_db
from .schemas import (
    ApprovalRequest,
    BugAnalysisResponse,
    BugReportIntake,
    BugReportResponse,
    BugReportSummary,
    FixAttemptResponse,
    ReanalyzeRequest,
)
from .service import bug_reports_service

# ---------- Public intake (called from deployed apps) ----------
public_router = APIRouter()


@public_router.post("/{app_id}", response_model=BugReportResponse)
async def submit_bug_report(
    app_id: str,
    intake: BugReportIntake,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Receive a bug report from a deployed app.

    No auth required — deployed apps are CORS-allowlisted and the platform
    accepts unauthenticated reports so guests can file too. We DO try to
    decode the user's access_token cookie if present, to attribute the report.
    """
    # Verify app exists and has the widget enabled (else this endpoint should be a no-op).
    app = (await db.execute(select(App).where(App.id == app_id))).scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="App not found")
    if not app.bug_widget_enabled:
        raise HTTPException(status_code=403, detail="Bug reporting is disabled for this app")

    # Optional reporter attribution from the cookie or bearer header
    reporter_user_id: str | None = None
    auth_header = request.headers.get("authorization")
    token = None
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
    if not token:
        token = request.cookies.get("access_token")
    if token:
        payload = auth_service.decode_access_token(token)
        if payload:
            reporter_user_id = payload.get("sub")

    report = await bug_reports_service.create_report(
        db,
        app_id=app_id,
        title=intake.title,
        description=intake.description,
        version=intake.version,
        deployment_id=intake.deployment_id,
        reporter_user_id=reporter_user_id,
        reporter_label=intake.reporter_label,
        captured_context=intake.captured_context.model_dump() if intake.captured_context else {},
        screenshot_data_url=intake.screenshot_data_url,
    )
    return _to_report_response(db, report, analyses=[], attempts=[])


# ---------- Admin endpoints ----------
admin_router = APIRouter()


@admin_router.get("", response_model=list[BugReportSummary])
async def list_reports(
    app_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    reports = await bug_reports_service.list_reports(db, app_id=app_id)
    summaries: list[BugReportSummary] = []
    # Pull all referenced apps in one query for efficiency.
    app_ids = {r.app_id for r in reports}
    apps: dict[str, App] = {}
    if app_ids:
        rows = await db.execute(select(App).where(App.id.in_(app_ids)))
        apps = {a.id: a for a in rows.scalars().all()}

    for r in reports:
        latest = await bug_reports_service.latest_analysis(db, r.id)
        a = apps.get(r.app_id)
        summaries.append(BugReportSummary(
            id=r.id,
            app_id=r.app_id,
            app_name=a.name if a else None,
            version=r.version,
            title=r.title,
            status=r.status,
            risk_level=latest.risk_level if latest else None,
            auto_approve_enabled=bool(a and a.bug_fix_auto_approve_max_risk and a.bug_fix_auto_approve_max_risk != "none"),
            reporter_label=r.reporter_label,
            created_at=r.created_at,
        ))
    return summaries


@admin_router.get("/{report_id}", response_model=BugReportResponse)
async def get_report(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    report = await bug_reports_service.get_report(db, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Bug report not found")
    analyses = await bug_reports_service.list_analyses(db, report_id)
    attempts = await bug_reports_service.list_attempts(db, report_id)
    return _to_report_response(db, report, analyses=analyses, attempts=attempts)


@admin_router.get("/{report_id}/screenshot")
async def get_screenshot(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    path = await bug_reports_service.screenshot_path(db, report_id)
    if not path:
        raise HTTPException(status_code=404, detail="No screenshot")
    return FileResponse(str(path), media_type="image/png")


@admin_router.post("/{report_id}/reanalyze", response_model=BugReportResponse)
async def reanalyze(
    report_id: str,
    request: ReanalyzeRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    report = await bug_reports_service.reanalyze(db, report_id, note=request.note)
    if not report:
        raise HTTPException(status_code=404, detail="Bug report not found")
    analyses = await bug_reports_service.list_analyses(db, report_id)
    attempts = await bug_reports_service.list_attempts(db, report_id)
    return _to_report_response(db, report, analyses=analyses, attempts=attempts)


@admin_router.post("/{report_id}/approve", response_model=FixAttemptResponse)
async def approve(
    report_id: str,
    request: ApprovalRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    attempt = await bug_reports_service.approve(
        db, report_id, user_id=user.id, analysis_id=request.analysis_id,
    )
    if not attempt:
        raise HTTPException(status_code=404, detail="Bug report or analysis not found")
    return attempt


@admin_router.post("/{report_id}/reject", response_model=BugReportResponse)
async def reject(
    report_id: str,
    request: ReanalyzeRequest,  # reuse — just `note` field
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    report = await bug_reports_service.reject(db, report_id, user_id=user.id, note=request.note)
    if not report:
        raise HTTPException(status_code=404, detail="Bug report not found")
    analyses = await bug_reports_service.list_analyses(db, report_id)
    attempts = await bug_reports_service.list_attempts(db, report_id)
    return _to_report_response(db, report, analyses=analyses, attempts=attempts)


def _to_report_response(db, report, *, analyses, attempts) -> BugReportResponse:
    return BugReportResponse(
        id=report.id,
        app_id=report.app_id,
        version=report.version,
        deployment_id=report.deployment_id,
        reporter_user_id=report.reporter_user_id,
        reporter_label=report.reporter_label,
        title=report.title,
        description=report.description,
        captured_context=report.captured_context or {},
        screenshot_url=(f"/api/bug-reports/{report.id}/screenshot" if report.screenshot_path else None),
        status=report.status,
        error=report.error,
        created_at=report.created_at,
        updated_at=report.updated_at,
        analyses=[BugAnalysisResponse.model_validate(a) for a in analyses],
        attempts=[FixAttemptResponse.model_validate(a) for a in attempts],
    )
