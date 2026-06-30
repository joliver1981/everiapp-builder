import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.database import Base
from src.deployments.models import Deployment, DeploymentTarget
from src.deployments.service import DeploymentsService


@pytest.fixture
async def db():
    # Each test gets its own in-memory DB.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        # Import all models so dependencies exist.
        from src.auth.models import RefreshToken, User  # noqa
        from src.apps.models import App, AppPermission, AppSetting, AppVersion, Conversation, Message  # noqa
        from src.secrets.models import AuditLog, Secret  # noqa
        from src.marketplace.models import MarketplaceListing  # noqa
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as s:
        yield s
    await engine.dispose()


def _target(start=9100, end=9102):
    return DeploymentTarget(
        id="t1", name="t", kind="agent", host="x", port=8765,
        port_range_start=start, port_range_end=end, environment="dev",
        is_active=True, extra_config={},
    )


@pytest.mark.asyncio
async def test_allocates_lowest_free(db: AsyncSession):
    target = _target(9100, 9102)
    db.add(target)
    await db.commit()

    svc = DeploymentsService()
    p1 = await svc._allocate_port(db, target)
    assert p1 == 9100

    # Simulate that port being in use by an active deployment
    db.add(Deployment(
        id="d1", app_id="a1", version=1, target_id=target.id,
        allocated_port=9100, status="running", deployed_by="u1",
    ))
    await db.commit()

    p2 = await svc._allocate_port(db, target)
    assert p2 == 9101


@pytest.mark.asyncio
async def test_skips_only_active_status(db: AsyncSession):
    target = _target(9100, 9100)
    db.add(target)
    db.add(Deployment(
        id="d-stopped", app_id="a1", version=1, target_id=target.id,
        allocated_port=9100, status="stopped", deployed_by="u1",
    ))
    await db.commit()

    svc = DeploymentsService()
    # The stopped deployment should NOT hold the port
    assert await svc._allocate_port(db, target) == 9100


@pytest.mark.asyncio
async def test_exhaustion_raises(db: AsyncSession):
    target = _target(9100, 9100)
    db.add(target)
    db.add(Deployment(
        id="d1", app_id="a1", version=1, target_id=target.id,
        allocated_port=9100, status="running", deployed_by="u1",
    ))
    await db.commit()

    svc = DeploymentsService()
    with pytest.raises(RuntimeError):
        await svc._allocate_port(db, target)


@pytest.mark.asyncio
async def test_preferred_port_honoured_when_free(db: AsyncSession):
    """The deploy flow passes the prior deployment's port as `preferred` so the
    new version lands on the same URL. Verify _allocate_port respects it."""
    target = _target(9100, 9110)
    db.add(target)
    await db.commit()

    svc = DeploymentsService()
    # Without preferred → lowest free (9100)
    assert await svc._allocate_port(db, target) == 9100
    # With preferred=9105 → should get 9105 even though 9100 is also free
    assert await svc._allocate_port(db, target, preferred=9105) == 9105


@pytest.mark.asyncio
async def test_preferred_port_falls_back_when_taken(db: AsyncSession):
    target = _target(9100, 9110)
    db.add(target)
    db.add(Deployment(
        id="d1", app_id="a1", version=1, target_id=target.id,
        allocated_port=9105, status="running", deployed_by="u1",
    ))
    await db.commit()

    svc = DeploymentsService()
    # Preferred 9105 is taken → fall back to lowest free (9100)
    assert await svc._allocate_port(db, target, preferred=9105) == 9100


@pytest.mark.asyncio
async def test_preferred_port_outside_range_ignored(db: AsyncSession):
    target = _target(9100, 9110)
    db.add(target)
    await db.commit()

    svc = DeploymentsService()
    # Preferred 9999 isn't in [9100,9110] → fall back to lowest free
    assert await svc._allocate_port(db, target, preferred=9999) == 9100
