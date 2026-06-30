"""Sanity tests for the connections + datasets data model.

These are model-level tests (no HTTP). HTTP integration coverage lands with
the router PRs that follow.
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.database import Base


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        # Import every model so create_all sees the full graph (FKs from
        # app_dataset_bindings reach into apps + datasets).
        from src.auth.models import RefreshToken, User  # noqa: F401
        from src.apps.models import App, AppPermission, AppSetting, AppVersion, Conversation, Message  # noqa: F401
        from src.secrets.models import AuditLog, Secret  # noqa: F401
        from src.marketplace.models import MarketplaceListing  # noqa: F401
        from src.deployments.models import Deployment, DeploymentTarget  # noqa: F401
        from src.bug_reports.models import BugAnalysis, BugReport, FixAttempt  # noqa: F401
        from src.connections.models import Connection  # noqa: F401
        from src.datasets.models import AppDatasetBinding, Dataset  # noqa: F401
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_create_connection_round_trip(db: AsyncSession):
    from src.connections.models import Connection

    c = Connection(
        name="prod-postgres",
        description="prod analytics replica",
        kind="sql",
        config={"dialect": "postgres", "host": "db.example.com", "port": 5432, "database": "analytics"},
        credential_secret_ref="prod_postgres_password",
        created_by="user-1",
    )
    db.add(c)
    await db.commit()

    fetched = (await db.execute(select(Connection).where(Connection.name == "prod-postgres"))).scalar_one()
    assert fetched.kind == "sql"
    assert fetched.config["dialect"] == "postgres"
    # Defaults applied
    assert fetched.default_row_limit == 500000
    assert fetched.default_timeout_seconds == 30
    assert fetched.read_only is True


@pytest.mark.asyncio
async def test_create_dataset_round_trip(db: AsyncSession):
    from src.connections.models import Connection
    from src.datasets.models import Dataset

    conn = Connection(name="c1", kind="sql", config={}, created_by="user-1")
    db.add(conn)
    await db.flush()

    ds = Dataset(
        name="recent_orders",
        description="Recent orders for a customer",
        connection_id=conn.id,
        kind="query",
        definition={"sql": "SELECT id, total FROM orders WHERE customer_id = :customer_id LIMIT 100"},
        parameter_schema={
            "type": "object",
            "properties": {"customer_id": {"type": "string"}},
            "required": ["customer_id"],
        },
        output_schema={
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "total": {"type": "number"}},
            },
        },
        owner_id="user-1",
        visibility="org",
    )
    db.add(ds)
    await db.commit()

    fetched = (await db.execute(select(Dataset).where(Dataset.name == "recent_orders"))).scalar_one()
    assert fetched.connection_id == conn.id
    assert fetched.kind == "query"
    assert fetched.parameter_schema["required"] == ["customer_id"]
    assert fetched.visibility == "org"
    # Override fields default to None
    assert fetched.row_limit_override is None
    assert fetched.timeout_override is None


@pytest.mark.asyncio
async def test_app_dataset_binding_round_trip(db: AsyncSession):
    from src.apps.models import App
    from src.connections.models import Connection
    from src.datasets.models import AppDatasetBinding, Dataset

    app = App(name="my-app", description="", created_by="user-1")
    conn = Connection(name="c1", kind="sql", config={}, created_by="user-1")
    db.add_all([app, conn])
    await db.flush()

    ds = Dataset(name="d1", connection_id=conn.id, kind="query", definition={"sql": "SELECT 1"}, owner_id="user-1")
    db.add(ds)
    await db.flush()

    binding = AppDatasetBinding(app_id=app.id, dataset_id=ds.id)
    db.add(binding)
    await db.commit()

    fetched = (
        await db.execute(
            select(AppDatasetBinding).where(
                AppDatasetBinding.app_id == app.id,
                AppDatasetBinding.dataset_id == ds.id,
            )
        )
    ).scalar_one()
    assert fetched.app_id == app.id
    assert fetched.dataset_id == ds.id
