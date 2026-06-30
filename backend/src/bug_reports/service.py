import asyncio
import base64
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..apps.models import App
from ..config import settings
from ..database import async_session
from ..deployments.models import Deployment
from ..deployments.service import deployments_service
from ..secrets.models import AuditLog
from ..versions.service import versions_service
from .analyzer import AnalysisResult, run_analysis
from .models import BugAnalysis, BugReport, FixAttempt

logger = logging.getLogger(__name__)


# none < low < medium — used to gate auto-approval
_RISK_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}


def _data_url_to_png_bytes(data_url: str) -> bytes | None:
    """Decode a `data:image/png;base64,...` payload. Returns None on any malformed input."""
    if not data_url:
        return None
    m = re.match(r"data:image/(?:png|jpeg|jpg|webp);base64,(.+)", data_url, re.IGNORECASE)
    if not m:
        return None
    try:
        return base64.b64decode(m.group(1), validate=True)
    except (ValueError, TypeError):
        return None


def _bug_report_dir(report_id: str) -> Path:
    return Path(settings.app_data_dir).resolve().parent / "bug_reports" / report_id


class BugReportsService:
    # ---------- Intake ----------

    async def create_report(
        self,
        db: AsyncSession,
        *,
        app_id: str,
        title: str,
        description: str,
        version: int | None,
        deployment_id: str | None,
        reporter_user_id: str | None,
        reporter_label: str | None,
        captured_context: dict | None,
        screenshot_data_url: str | None,
    ) -> BugReport:
        report = BugReport(
            app_id=app_id,
            version=version,
            deployment_id=deployment_id,
            reporter_user_id=reporter_user_id,
            reporter_label=reporter_label,
            title=title,
            description=description,
            captured_context=captured_context or {},
            status="new",
        )
        db.add(report)
        await db.flush()  # populate id

        # Persist screenshot to disk if provided
        png = _data_url_to_png_bytes(screenshot_data_url) if screenshot_data_url else None
        if png:
            d = _bug_report_dir(report.id)
            d.mkdir(parents=True, exist_ok=True)
            path = d / "screenshot.png"
            path.write_bytes(png)
            report.screenshot_path = str(path)

        await db.commit()
        await db.refresh(report)

        # Best-effort admin email (gated on smtp_enabled + notify_on_bug_report).
        try:
            from ..notifications.service import notify_bug_report
            summary = f"{title}\n\n{(description or '')[:500]}"
            await notify_bug_report(db, app_id, summary)
        except Exception:
            logger.exception("bug-report notification failed")

        # Kick off async analysis — never block the intake response
        asyncio.create_task(self._analyze_in_background(report.id))
        return report

    # ---------- Reads ----------

    async def list_reports(self, db: AsyncSession, app_id: str | None = None) -> list[BugReport]:
        query = select(BugReport).order_by(BugReport.created_at.desc())
        if app_id:
            query = query.where(BugReport.app_id == app_id)
        result = await db.execute(query)
        return list(result.scalars().all())

    async def get_report(self, db: AsyncSession, report_id: str) -> BugReport | None:
        return (
            await db.execute(select(BugReport).where(BugReport.id == report_id))
        ).scalar_one_or_none()

    async def list_analyses(self, db: AsyncSession, report_id: str) -> list[BugAnalysis]:
        result = await db.execute(
            select(BugAnalysis)
            .where(BugAnalysis.bug_report_id == report_id)
            .order_by(BugAnalysis.created_at.desc())
        )
        return list(result.scalars().all())

    async def latest_analysis(self, db: AsyncSession, report_id: str) -> BugAnalysis | None:
        result = await db.execute(
            select(BugAnalysis)
            .where(BugAnalysis.bug_report_id == report_id)
            .order_by(BugAnalysis.created_at.desc())
            .limit(1)
        )
        return result.scalars().first()

    async def list_attempts(self, db: AsyncSession, report_id: str) -> list[FixAttempt]:
        result = await db.execute(
            select(FixAttempt)
            .where(FixAttempt.bug_report_id == report_id)
            .order_by(FixAttempt.created_at.desc())
        )
        return list(result.scalars().all())

    async def screenshot_path(self, db: AsyncSession, report_id: str) -> Path | None:
        report = await self.get_report(db, report_id)
        if not report or not report.screenshot_path:
            return None
        p = Path(report.screenshot_path)
        return p if p.exists() else None

    # ---------- Analysis ----------

    async def _analyze_in_background(self, report_id: str) -> None:
        async with async_session() as db:
            report = await self.get_report(db, report_id)
            if not report:
                return
            report.status = "analyzing"
            await db.commit()

        result: AnalysisResult
        try:
            async with async_session() as db:
                result = await run_analysis(
                    db,
                    app_id=report.app_id,
                    version=report.version,
                    bug_title=report.title,
                    bug_description=report.description,
                    captured_context=report.captured_context or {},
                )
        except Exception as e:
            logger.exception("Analyzer crashed for report %s", report_id)
            result = AnalysisResult(error=str(e))

        async with async_session() as db:
            report = await self.get_report(db, report_id)
            if not report:
                return

            if result.error:
                report.status = "failed"
                report.error = result.error
                await db.commit()
                return

            analysis = BugAnalysis(
                bug_report_id=report.id,
                diagnosis=result.diagnosis,
                root_cause=result.root_cause,
                proposed_files=result.proposed_files,
                risk_level=result.risk_level,
                risk_rationale=result.risk_rationale,
                llm_model=result.llm_model,
                raw_response=result.raw_response,
            )
            db.add(analysis)
            report.status = "analyzed"
            report.error = None
            await db.commit()
            await db.refresh(analysis)

        # Auto-approve gating: based on the App's max-allowed risk
        await self._maybe_auto_approve(report_id, analysis.id)

    async def reanalyze(self, db: AsyncSession, report_id: str, note: str = "") -> BugReport | None:
        report = await self.get_report(db, report_id)
        if not report:
            return None
        report.status = "analyzing"
        await db.commit()

        async def go():
            try:
                async with async_session() as inner:
                    result = await run_analysis(
                        inner,
                        app_id=report.app_id,
                        version=report.version,
                        bug_title=report.title,
                        bug_description=report.description,
                        captured_context=report.captured_context or {},
                        extra_note=note,
                    )
            except Exception as e:
                logger.exception("Reanalyzer crashed for %s", report_id)
                result = AnalysisResult(error=str(e))

            async with async_session() as inner:
                fresh = await self.get_report(inner, report_id)
                if not fresh:
                    return
                if result.error:
                    fresh.status = "failed"
                    fresh.error = result.error
                    await inner.commit()
                    return
                analysis = BugAnalysis(
                    bug_report_id=fresh.id,
                    diagnosis=result.diagnosis,
                    root_cause=result.root_cause,
                    proposed_files=result.proposed_files,
                    risk_level=result.risk_level,
                    risk_rationale=result.risk_rationale,
                    llm_model=result.llm_model,
                    raw_response=result.raw_response,
                )
                inner.add(analysis)
                fresh.status = "analyzed"
                fresh.error = None
                await inner.commit()
                await inner.refresh(analysis)
            await self._maybe_auto_approve(report_id, analysis.id)

        asyncio.create_task(go())
        return report

    # ---------- Approval / auto-approval ----------

    async def _maybe_auto_approve(self, report_id: str, analysis_id: str) -> None:
        async with async_session() as db:
            report = await self.get_report(db, report_id)
            if not report:
                return
            app = (await db.execute(select(App).where(App.id == report.app_id))).scalar_one_or_none()
            if not app:
                return

            threshold = app.bug_fix_auto_approve_max_risk or "none"
            analysis = (
                await db.execute(select(BugAnalysis).where(BugAnalysis.id == analysis_id))
            ).scalar_one_or_none()
            if not analysis:
                return

            if not is_risk_within_threshold(analysis.risk_level, threshold):
                return  # waits for human approval
            if not analysis.proposed_files:
                return  # nothing to apply

        # We're inside the threshold AND have a proposed fix → fire the fix flow.
        logger.info("Auto-approving fix for report %s (risk=%s, threshold=%s)",
                    report_id, analysis.risk_level, threshold)
        await self._run_fix(report_id, analysis_id, approved_by=None, auto_approved=True)

    async def approve(
        self,
        db: AsyncSession,
        report_id: str,
        user_id: str,
        analysis_id: str | None = None,
    ) -> FixAttempt | None:
        report = await self.get_report(db, report_id)
        if not report:
            return None
        if analysis_id is None:
            latest = await self.latest_analysis(db, report_id)
            if not latest:
                return None
            analysis_id = latest.id

        # Audit log the human approval (auto-approvals are logged by the apply path)
        db.add(AuditLog(
            user_id=user_id,
            action="bug_report.approve",
            resource_type="bug_report",
            resource_id=report.id,
            details=f"Approved analysis {analysis_id} for report '{report.title}'",
        ))
        await db.commit()

        # Kick the apply flow in the background so the API call returns fast.
        asyncio.create_task(self._run_fix(report_id, analysis_id, approved_by=user_id, auto_approved=False))
        # Return a synthetic attempt to the caller so the UI knows we started.
        return FixAttempt(
            id="-pending-",
            bug_report_id=report_id,
            analysis_id=analysis_id,
            status="pending",
            auto_approved=False,
            approved_by=user_id,
            approved_at=datetime.now(timezone.utc),
        )

    async def reject(self, db: AsyncSession, report_id: str, user_id: str, note: str = "") -> BugReport | None:
        report = await self.get_report(db, report_id)
        if not report:
            return None
        report.status = "rejected"
        if note:
            report.error = f"Rejected: {note}"
        db.add(AuditLog(
            user_id=user_id,
            action="bug_report.reject",
            resource_type="bug_report",
            resource_id=report.id,
            details=note or "(no note)",
        ))
        await db.commit()
        await db.refresh(report)
        return report

    # ---------- Apply / build / redeploy ----------

    async def _run_fix(
        self,
        report_id: str,
        analysis_id: str,
        *,
        approved_by: str | None,
        auto_approved: bool,
    ) -> None:
        attempt_id: str | None = None
        try:
            async with async_session() as db:
                report = await self.get_report(db, report_id)
                analysis = (
                    await db.execute(select(BugAnalysis).where(BugAnalysis.id == analysis_id))
                ).scalar_one_or_none()
                if not report or not analysis:
                    return

                attempt = FixAttempt(
                    bug_report_id=report.id,
                    analysis_id=analysis.id,
                    base_version=report.version,
                    status="applying",
                    auto_approved=auto_approved,
                    approved_by=approved_by,
                    approved_at=datetime.now(timezone.utc),
                )
                db.add(attempt)
                report.status = "applying"
                await db.commit()
                await db.refresh(attempt)
                attempt_id = attempt.id

            # 1. Apply file changes to draft
            await self._apply_file_changes(report_id, analysis.proposed_files)

            # 2. Publish a new version (this also takes the snapshot the builder needs)
            async with async_session() as db:
                report = await self.get_report(db, report_id)
                report.status = "testing"
                await db.commit()

                published = await versions_service.publish(
                    db,
                    report.app_id,
                    user_id=approved_by or "system-auto-fix",
                    notes=f"Auto-fix for bug report {report.id}: {report.title[:120]}",
                )

            # 3. Build (this is our "test" gate — npm run build must succeed)
            from ..deployments import builder
            try:
                await builder.build_app(report.app_id, published.version, force=True)
            except Exception as e:
                async with async_session() as db:
                    await self._mark_attempt_failed(db, attempt_id, f"Build failed: {e}")
                return

            # 4. Find the deployment we should redeploy to (same target as the bug report's
            #    deployment_id if we have one; otherwise pick the most recent running deployment).
            async with async_session() as db:
                report = await self.get_report(db, report_id)
                target_id = await self._pick_redeploy_target(db, report)
                if not target_id:
                    # Fix was applied + version published, but we can't redeploy without a target.
                    # This is a PARTIAL outcome — the autonomous workflow didn't finish.
                    # Mark it failed so the UI surfaces it; the published version is recoverable
                    # from the error message and the user can deploy manually from the builder.
                    attempt = (
                        await db.execute(select(FixAttempt).where(FixAttempt.id == attempt_id))
                    ).scalar_one_or_none()
                    error_msg = (
                        f"Published v{published.version} but no active deployment target was "
                        f"available for this app. Deploy the new version manually from the "
                        f"Deployments panel in the App Builder."
                    )
                    if attempt:
                        attempt.new_version = published.version
                        attempt.status = "failed"
                        attempt.error = error_msg
                    if report:
                        report.status = "failed"
                        report.error = error_msg
                    await db.commit()
                    return

                report.status = "deploying"
                await db.commit()
                deployment = await deployments_service.deploy(
                    db,
                    app_id=report.app_id,
                    version=published.version,
                    target_id=target_id,
                    user_id=approved_by or "system-auto-fix",
                )

            # 5. Wait briefly for the deployment to settle, then record the outcome.
            #    We don't poll forever — the deployments health loop will reflect the truth.
            await asyncio.sleep(5)

            async with async_session() as db:
                attempt = (
                    await db.execute(select(FixAttempt).where(FixAttempt.id == attempt_id))
                ).scalar_one_or_none()
                if attempt:
                    attempt.new_version = published.version
                    attempt.deployment_id = deployment.id
                    attempt.status = "succeeded"
                report = await self.get_report(db, report_id)
                if report:
                    report.status = "resolved"
                await db.commit()

        except Exception as e:
            logger.exception("Fix flow crashed for report %s", report_id)
            if attempt_id:
                async with async_session() as db:
                    await self._mark_attempt_failed(db, attempt_id, str(e))
            else:
                async with async_session() as db:
                    report = await self.get_report(db, report_id)
                    if report:
                        report.status = "failed"
                        report.error = str(e)[:1000]
                        await db.commit()

    async def _apply_file_changes(self, report_id: str, files: list[dict]) -> None:
        """Write proposed file changes to the app's draft directory."""
        async with async_session() as db:
            report = await self.get_report(db, report_id)
            if not report:
                return
            app_id = report.app_id

        draft_dir = (Path(settings.app_data_dir) / app_id / "draft" / "frontend").resolve()
        for f in files:
            rel = f.get("path", "")
            action = f.get("action", "update")
            content = f.get("content", "")
            if not rel:
                continue
            target = (draft_dir / rel).resolve()
            try:
                target.relative_to(draft_dir)
            except ValueError:
                logger.warning("Skipping file outside draft: %s", rel)
                continue
            if action == "delete":
                if target.exists():
                    target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    async def _pick_redeploy_target(self, db: AsyncSession, report: BugReport) -> str | None:
        """Decision: fix lands on the SAME target the bug came from, when we know it."""
        if report.deployment_id:
            d = (
                await db.execute(select(Deployment).where(Deployment.id == report.deployment_id))
            ).scalar_one_or_none()
            if d:
                return d.target_id
        # Otherwise: most-recent running deployment for this app
        result = await db.execute(
            select(Deployment)
            .where(Deployment.app_id == report.app_id, Deployment.status == "running")
            .order_by(Deployment.started_at.desc())
        )
        d = result.scalars().first()
        return d.target_id if d else None

    async def _mark_attempt_failed(self, db: AsyncSession, attempt_id: str, error: str) -> None:
        attempt = (
            await db.execute(select(FixAttempt).where(FixAttempt.id == attempt_id))
        ).scalar_one_or_none()
        if attempt:
            attempt.status = "failed"
            attempt.error = error[:1000]
            report = (
                await db.execute(select(BugReport).where(BugReport.id == attempt.bug_report_id))
            ).scalar_one_or_none()
            if report:
                report.status = "failed"
                report.error = error[:1000]
        await db.commit()


def is_risk_within_threshold(risk_level: str, threshold: str) -> bool:
    """Return True if `risk_level` is at or below `threshold` (so auto-approve fires).

    threshold == "none" means human approval is always required.
    threshold == "low" means only risk_level == "low" auto-approves.
    threshold == "medium" means "low" and "medium" auto-approve. "high" never auto-approves.
    """
    threshold = (threshold or "none").lower()
    if threshold == "none":
        return False
    risk_rank = _RISK_RANK.get(risk_level, 99)
    threshold_rank = _RISK_RANK.get(threshold, 0)
    if risk_rank >= _RISK_RANK["high"]:
        return False  # high risk is never auto-approved
    return risk_rank <= threshold_rank


bug_reports_service = BugReportsService()
