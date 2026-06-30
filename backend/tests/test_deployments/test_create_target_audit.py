"""Regression test for the audit_log.resource_id NULL bug.

Bug: `create_target` (and similar create-paths) built an AuditLog row with
`resource_id=target.id` BEFORE flush, so target.id was still None (SQLAlchemy
only runs `default=lambda` at flush time). The DB then rejected the row because
`audit_logs.resource_id` is NOT NULL.

Fix: call `await db.flush()` between adding the target and adding the audit log.
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.database import Base
from src.deployments.models import DeploymentTarget
from src.deployments.schemas import TargetCreate
from src.deployments.service import deployments_service
from src.secrets.models import AuditLog, Secret


async def _make_agent_token_secret(db: AsyncSession) -> str:
    """Create a Secret of category 'agent_token' so agent-kind targets satisfy the
    new credential requirement. Returns the secret id."""
    secret = Secret(name="test-token", category="agent_token", encrypted_value="x")
    db.add(secret)
    await db.flush()
    return secret.id


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        from src.auth.models import RefreshToken, User  # noqa
        from src.apps.models import App, AppPermission, AppSetting, AppVersion, Conversation, Message  # noqa
        from src.secrets.models import Secret  # noqa
        from src.marketplace.models import MarketplaceListing  # noqa
        from src.deployments.models import Deployment  # noqa
        from src.bug_reports.models import BugAnalysis, BugReport, FixAttempt  # noqa
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_create_target_writes_audit_log_with_populated_resource_id(db: AsyncSession):
    """Smoke: create_target commits successfully AND the audit row has a real id."""
    cred_id = await _make_agent_token_secret(db)
    data = TargetCreate(
        name="local-agent",
        kind="agent",
        host="localhost",
        port=8765,
        port_range_start=9100,
        port_range_end=9120,
        environment="dev",
        credential_secret_id=cred_id,
    )
    target = await deployments_service.create_target(db, data, user_id="user-1")

    # The target itself got an id
    assert target.id is not None
    assert len(target.id) == 36  # uuid4

    # And there's an audit_log row pointing at it
    rows = (await db.execute(
        select(AuditLog).where(AuditLog.action == "deployment_target.create")
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].resource_id == target.id, (
        f"audit_log.resource_id should equal the new target's id, "
        f"got {rows[0].resource_id!r} vs {target.id!r}"
    )
    assert rows[0].user_id == "user-1"


@pytest.mark.asyncio
async def test_create_target_target_row_exists_after_commit(db: AsyncSession):
    """Confirm the deployment_target row is queryable post-commit (catches the rollback)."""
    cred_id = await _make_agent_token_secret(db)
    data = TargetCreate(
        name="t-1", kind="agent", host="localhost", port=8765,
        port_range_start=9100, port_range_end=9120, environment="dev",
        credential_secret_id=cred_id,
    )
    target = await deployments_service.create_target(db, data, user_id="user-1")
    fetched = (await db.execute(
        select(DeploymentTarget).where(DeploymentTarget.id == target.id)
    )).scalar_one_or_none()
    assert fetched is not None
    assert fetched.name == "t-1"
