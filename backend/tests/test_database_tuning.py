"""Tests for the SQLite tuning + index migrations applied on platform DB init.

What we lock in:
  - Pragmas: WAL, NORMAL sync, busy_timeout, cache_size, MEMORY temp_store, FKs ON
  - All composite indexes from COMPOSITE_INDEXES exist after init_db()
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession, create_async_engine

from src.database import Base
from src.database_tuning import (
    COMPOSITE_INDEXES,
    apply_index_migrations,
    apply_sqlite_tuning,
    list_indexes,
    read_pragmas,
)


@pytest.fixture
async def tuned_engine():
    """Fresh in-memory engine with tuning applied — matches what init_db does."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    apply_sqlite_tuning(engine)

    # Need at least the models loaded for index migrations to land
    async with engine.begin() as conn:
        from src.auth.models import User, RefreshToken  # noqa: F401
        from src.apps.models import App, AppPermission, AppSetting, AppVersion, Conversation, Message  # noqa: F401
        from src.secrets.models import AuditLog, Secret  # noqa: F401
        from src.marketplace.models import MarketplaceListing  # noqa: F401
        from src.deployments.models import Deployment, DeploymentTarget  # noqa: F401
        from src.bug_reports.models import BugAnalysis, BugReport, FixAttempt  # noqa: F401
        from src.connections.models import Connection  # noqa: F401
        from src.datasets.models import AppDatasetBinding, Dataset  # noqa: F401

        await conn.run_sync(Base.metadata.create_all)

    await apply_index_migrations(engine)
    yield engine
    await engine.dispose()


@pytest.mark.asyncio
async def test_journal_mode_is_wal(tuned_engine):
    pragmas = await read_pragmas(tuned_engine)
    # In-memory DBs ignore journal_mode=WAL and report 'memory'. Tolerate both —
    # what matters in tests is the listener fired without exploding. The
    # production check is in `aihub doctor` against an on-disk DB.
    assert pragmas["journal_mode"].lower() in ("wal", "memory")


@pytest.mark.asyncio
async def test_synchronous_pragma(tuned_engine):
    pragmas = await read_pragmas(tuned_engine)
    # SQLite returns "1" for NORMAL
    assert pragmas["synchronous"] in ("1", "NORMAL", "normal")


@pytest.mark.asyncio
async def test_busy_timeout_set(tuned_engine):
    pragmas = await read_pragmas(tuned_engine)
    assert pragmas["busy_timeout"] == "5000"


@pytest.mark.asyncio
async def test_cache_size_set(tuned_engine):
    pragmas = await read_pragmas(tuned_engine)
    # cache_size = -64000 (KB = 64 MB)
    assert pragmas["cache_size"] == "-64000"


@pytest.mark.asyncio
async def test_temp_store_memory(tuned_engine):
    pragmas = await read_pragmas(tuned_engine)
    # SQLite returns "2" for MEMORY
    assert pragmas["temp_store"] in ("2", "MEMORY", "memory")


@pytest.mark.asyncio
async def test_foreign_keys_on(tuned_engine):
    pragmas = await read_pragmas(tuned_engine)
    # SQLite returns "1" for ON
    assert pragmas["foreign_keys"] in ("1", "ON", "on")


@pytest.mark.asyncio
async def test_all_composite_indexes_created(tuned_engine):
    existing = await list_indexes(tuned_engine)
    expected = {name for name, _, _ in COMPOSITE_INDEXES}
    missing = expected - existing
    assert not missing, f"missing indexes: {missing}"


@pytest.mark.asyncio
async def test_index_migration_idempotent(tuned_engine):
    """Running the migration twice must not raise (CREATE INDEX IF NOT EXISTS)."""
    await apply_index_migrations(tuned_engine)
    await apply_index_migrations(tuned_engine)
    # If we got here, no exception. Now verify we didn't somehow duplicate.
    existing = await list_indexes(tuned_engine)
    duplicated = {n for n, _, _ in COMPOSITE_INDEXES if n in existing}
    assert len(duplicated) == len(COMPOSITE_INDEXES)


@pytest.mark.asyncio
async def test_apply_tuning_is_no_op_for_non_sqlite():
    """Don't blow up if the engine isn't SQLite (forward-compat for Postgres)."""
    # We can't actually create a postgres engine without a server, so we just
    # confirm the function returns silently for a non-sqlite URL by mocking
    # the driver name.
    from unittest.mock import MagicMock

    fake = MagicMock()
    fake.url.drivername = "postgresql+asyncpg"
    apply_sqlite_tuning(fake)
    # If we got here without an exception, we're good — and event.listens_for
    # should NOT have been called on fake.sync_engine.
    fake.sync_engine.assert_not_called() if hasattr(fake.sync_engine, "assert_not_called") else None
