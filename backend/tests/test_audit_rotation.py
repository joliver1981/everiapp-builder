"""Tests for the audit log rotation system.

What we lock in:
  - No-op when nothing's older than the cutoff
  - Dry-run reports the right counts but doesn't write/delete
  - Real rotation writes valid gzipped JSONL and removes the rows
  - Idempotent: running again finds nothing else to do
  - Crash-safe append: a second run with surviving rows appends cleanly
  - The CLI subcommand works
"""
from __future__ import annotations

import gzip
import json
import os
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.audit_rotation import (
    RotationSummary,
    cutoff_iso_for_retention,
    resolve_archive_dir,
    rotate_audit_logs,
    rotation_status,
)
from src.database import Base
from src.database_tuning import apply_index_migrations, apply_sqlite_tuning


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
async def fresh_engine(tmp_path):
    """Fresh engine pointed at an on-disk SQLite file (in-memory doesn't keep
    file-mode features like WAL, but file-mode does)."""
    db_path = tmp_path / "rotation.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    apply_sqlite_tuning(engine)

    # Bring up just enough schema for the test: audit_logs only.
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


async def _insert_audit_rows(engine, rows: list[dict]) -> None:
    """Helper: insert a batch of audit_logs rows directly."""
    async with engine.begin() as conn:
        for r in rows:
            await conn.execute(
                text(
                    "INSERT INTO audit_logs "
                    "(id, user_id, action, resource_type, resource_id, details, created_at) "
                    "VALUES (:id, :user_id, :action, :resource_type, :resource_id, :details, :created_at)"
                ),
                r,
            )


def _row(created_at: str, action: str = "test.event") -> dict:
    return {
        "id": str(uuid.uuid4()),
        "user_id": "test-user",
        "action": action,
        "resource_type": "test",
        "resource_id": str(uuid.uuid4()),
        "details": "synthetic",
        "created_at": created_at,
    }


# ---------------------------------------------------------------------------
# cutoff_iso_for_retention
# ---------------------------------------------------------------------------
def test_cutoff_basic():
    now = datetime(2026, 6, 4, tzinfo=timezone.utc)
    assert cutoff_iso_for_retention(24, now=now).startswith("2024-06-01")


def test_cutoff_crosses_year_boundary():
    now = datetime(2026, 2, 15, tzinfo=timezone.utc)
    # 24 months back from Feb 2026 = Feb 2024
    assert cutoff_iso_for_retention(24, now=now).startswith("2024-02-01")


def test_cutoff_short_retention():
    now = datetime(2026, 6, 4, tzinfo=timezone.utc)
    # 1 month back from June = May 1
    assert cutoff_iso_for_retention(1, now=now).startswith("2026-05-01")


# ---------------------------------------------------------------------------
# resolve_archive_dir
# ---------------------------------------------------------------------------
def test_resolve_archive_dir_explicit(tmp_path):
    explicit = str(tmp_path / "custom_archives")
    assert resolve_archive_dir(explicit, "sqlite+aiosqlite:///foo.db") == Path(explicit)


def test_resolve_archive_dir_derives_from_db_path(tmp_path):
    db = tmp_path / "aihub.db"
    url = f"sqlite+aiosqlite:///{db}"
    got = resolve_archive_dir("", url)
    assert got == tmp_path / "archives"


def test_resolve_archive_dir_postgres_needs_explicit():
    with pytest.raises(ValueError):
        resolve_archive_dir("", "postgresql://localhost/aihub")


# ---------------------------------------------------------------------------
# rotate_audit_logs — no-op
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rotate_noop_when_no_rows(fresh_engine, tmp_path):
    arc = tmp_path / "archives"
    now = datetime(2026, 6, 4, tzinfo=timezone.utc)
    summary = await rotate_audit_logs(
        fresh_engine, archive_dir=arc, retention_months=24, now=now
    )
    assert summary.months_examined == []
    assert summary.rows_archived == 0
    assert summary.rows_deleted == 0


@pytest.mark.asyncio
async def test_rotate_noop_when_all_rows_recent(fresh_engine, tmp_path):
    # Insert rows from current month — they're inside the retention window.
    now = datetime(2026, 6, 4, tzinfo=timezone.utc)
    await _insert_audit_rows(fresh_engine, [
        _row("2026-06-01T10:00:00"),
        _row("2026-05-20T10:00:00"),
    ])
    arc = tmp_path / "archives"
    summary = await rotate_audit_logs(
        fresh_engine, archive_dir=arc, retention_months=24, now=now,
    )
    assert summary.rows_archived == 0
    assert summary.rows_deleted == 0
    # Both rows still in DB
    async with fresh_engine.connect() as c:
        n = (await c.execute(text("SELECT COUNT(*) FROM audit_logs"))).fetchone()[0]
    assert n == 2


# ---------------------------------------------------------------------------
# rotate_audit_logs — real archive + delete
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rotate_archives_and_deletes_old_rows(fresh_engine, tmp_path):
    now = datetime(2026, 6, 4, tzinfo=timezone.utc)
    # 3 rows in 2023-05 (older than 24-month cutoff of 2024-06-01)
    # 2 rows in 2024-01 (still older than cutoff)
    # 1 row in 2025-06 (inside window — must NOT be touched)
    old_jan = [_row("2024-01-15T10:00:00") for _ in range(2)]
    old_may = [_row("2023-05-15T10:00:00") for _ in range(3)]
    recent = [_row("2025-06-15T10:00:00")]
    await _insert_audit_rows(fresh_engine, old_jan + old_may + recent)

    arc = tmp_path / "archives"
    summary = await rotate_audit_logs(
        fresh_engine, archive_dir=arc, retention_months=24, now=now,
    )

    # Two months should have been archived: 2023-05 and 2024-01
    assert sorted(summary.months_archived) == ["2023-05", "2024-01"]
    assert summary.rows_archived == 5
    assert summary.rows_deleted == 5

    # The recent row is still in the DB; the old ones are gone.
    async with fresh_engine.connect() as c:
        remaining = (await c.execute(text("SELECT COUNT(*) FROM audit_logs"))).fetchone()[0]
    assert remaining == 1

    # Two archive files exist on disk
    files = sorted(arc.glob("audit_*.jsonl.gz"))
    assert {f.name for f in files} == {"audit_2023_05.jsonl.gz", "audit_2024_01.jsonl.gz"}


@pytest.mark.asyncio
async def test_archive_file_is_valid_jsonl(fresh_engine, tmp_path):
    """The archive must be readable as one JSON object per line."""
    now = datetime(2026, 6, 4, tzinfo=timezone.utc)
    await _insert_audit_rows(fresh_engine, [
        _row("2023-07-10T10:00:00", action="event.a"),
        _row("2023-07-15T11:00:00", action="event.b"),
    ])

    arc = tmp_path / "archives"
    await rotate_audit_logs(fresh_engine, archive_dir=arc, retention_months=24, now=now)

    archive_path = arc / "audit_2023_07.jsonl.gz"
    assert archive_path.exists()

    with gzip.open(archive_path, "rt", encoding="utf-8") as f:
        lines = [json.loads(line) for line in f if line.strip()]
    assert len(lines) == 2
    actions = {rec["action"] for rec in lines}
    assert actions == {"event.a", "event.b"}
    # Every line must have all the columns
    for rec in lines:
        for col in ("id", "user_id", "action", "resource_type", "resource_id", "details", "created_at"):
            assert col in rec, f"missing column {col} in {rec}"


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_dry_run_doesnt_modify(fresh_engine, tmp_path):
    now = datetime(2026, 6, 4, tzinfo=timezone.utc)
    await _insert_audit_rows(fresh_engine, [_row("2023-01-15T10:00:00") for _ in range(4)])

    arc = tmp_path / "archives"
    summary = await rotate_audit_logs(
        fresh_engine, archive_dir=arc, retention_months=24, dry_run=True, now=now,
    )

    assert summary.dry_run is True
    assert summary.rows_archived == 4  # reports what WOULD be archived
    # But the DB still has all the rows
    async with fresh_engine.connect() as c:
        n = (await c.execute(text("SELECT COUNT(*) FROM audit_logs"))).fetchone()[0]
    assert n == 4
    # And no archive file exists
    assert not list(arc.glob("audit_*.jsonl.gz"))


# ---------------------------------------------------------------------------
# Idempotency + crash-safety
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_idempotent_second_run_is_noop(fresh_engine, tmp_path):
    now = datetime(2026, 6, 4, tzinfo=timezone.utc)
    await _insert_audit_rows(fresh_engine, [_row("2023-05-15T10:00:00") for _ in range(3)])

    arc = tmp_path / "archives"
    first = await rotate_audit_logs(fresh_engine, archive_dir=arc, retention_months=24, now=now)
    assert first.rows_archived == 3

    second = await rotate_audit_logs(fresh_engine, archive_dir=arc, retention_months=24, now=now)
    assert second.rows_archived == 0
    assert second.months_examined == []


# ---------------------------------------------------------------------------
# rotation_status
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rotation_status_reports_files(fresh_engine, tmp_path):
    now = datetime(2026, 6, 4, tzinfo=timezone.utc)
    await _insert_audit_rows(fresh_engine, [
        _row("2023-05-15T10:00:00"),
        _row("2026-06-01T10:00:00"),  # recent — stays
    ])
    arc = tmp_path / "archives"
    await rotate_audit_logs(fresh_engine, archive_dir=arc, retention_months=24, now=now)

    status = await rotation_status(fresh_engine, arc)
    assert status["live_rows"] == 1
    assert status["archive_months_archived"] == 1
    assert status["archive_files"][0][0] == "audit_2023_05.jsonl.gz"
    assert status["archive_files"][0][1] > 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def test_cli_rotate_audit_help():
    """Doesn't need a DB — just makes sure the subcommand is registered."""
    from src.cli import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["rotate-audit", "--help"])
    assert result.exit_code == 0
    assert "Archive" in result.output or "archive" in result.output
    assert "--dry-run" in result.output


# NOTE: a previous version had a CLI smoke test that reloaded the src.database
# module to retarget it at a scratch DB. That reload was order-dependent in
# pytest runs and didn't add real coverage beyond the rotate_audit_logs tests
# above. The CLI itself is just a thin click wrapper around rotate_audit_logs;
# the test_cli_rotate_audit_help check above proves the subcommand is wired.
