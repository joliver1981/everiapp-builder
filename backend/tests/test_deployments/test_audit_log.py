"""Verify that security-relevant App field changes are audit-logged.

The bug-widget toggle opens a public intake endpoint; the auto-approve
threshold controls whether AI fixes apply without human review. Both
deserve an audit trail.
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.apps.models import App
from src.apps.schemas import AppUpdate
from src.apps.service import apps_service
from src.auth.models import User
from src.database import Base
from src.secrets.models import AuditLog


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        # Touch all model modules so they register before create_all.
        from src.apps.models import AppPermission, AppSetting, AppVersion, Conversation, Message  # noqa
        from src.secrets.models import Secret  # noqa
        from src.marketplace.models import MarketplaceListing  # noqa
        from src.deployments.models import Deployment, DeploymentTarget  # noqa
        from src.bug_reports.models import BugAnalysis, BugReport, FixAttempt  # noqa
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as s:
        yield s
    await engine.dispose()


async def _make_app(db: AsyncSession) -> str:
    db.add(User(id="u1", username="bob", display_name="Bob", email="bob@x.test", role="admin"))
    app = App(id="app-1", name="My App", created_by="u1")
    db.add(app)
    await db.commit()
    return app.id


@pytest.mark.asyncio
async def test_auto_approve_change_writes_audit_entry(db: AsyncSession):
    app_id = await _make_app(db)

    await apps_service.update_app(
        db, app_id,
        AppUpdate(bug_fix_auto_approve_max_risk="low"),
        user_id="u1",
    )

    rows = (await db.execute(select(AuditLog))).scalars().all()
    entries = [(r.action, r.resource_id, r.user_id, r.details) for r in rows]
    assert any(
        a == "app.bug_fix_auto_approve_max_risk.change"
        and resource == app_id
        and uid == "u1"
        and "none -> low" in details
        for a, resource, uid, details in entries
    ), f"missing audit entry, saw: {entries}"


@pytest.mark.asyncio
async def test_bug_widget_toggle_writes_audit_entry(db: AsyncSession):
    app_id = await _make_app(db)
    await apps_service.update_app(
        db, app_id,
        AppUpdate(bug_widget_enabled=True),
        user_id="u1",
    )
    rows = (await db.execute(select(AuditLog))).scalars().all()
    assert any(r.action == "app.bug_widget_enabled.change" for r in rows)


@pytest.mark.asyncio
async def test_no_audit_entry_when_value_unchanged(db: AsyncSession):
    """If you PUT the same value, we shouldn't spam the audit log."""
    app_id = await _make_app(db)
    await apps_service.update_app(
        db, app_id, AppUpdate(bug_widget_enabled=False), user_id="u1",
    )
    rows = (await db.execute(select(AuditLog))).scalars().all()
    assert all("bug_widget_enabled" not in r.action for r in rows)


@pytest.mark.asyncio
async def test_invalid_risk_value_rejected(db: AsyncSession):
    app_id = await _make_app(db)
    with pytest.raises(ValueError):
        await apps_service.update_app(
            db, app_id,
            AppUpdate(bug_fix_auto_approve_max_risk="high"),  # high is never allowed
            user_id="u1",
        )
