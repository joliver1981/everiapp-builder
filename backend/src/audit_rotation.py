"""Audit log rotation — keeps the platform DB small forever.

Rows older than `audit_retention_months` are streamed to a gzipped JSONL file
per month under `audit_archive_dir/`, then deleted from the live `audit_logs`
table. The archive format is one row per line so SIEMs (Splunk, Sentinel,
Elastic) can ingest it directly.

Design choices:
  - One file per (year, month): `audit_YYYY_MM.jsonl.gz`. Keeps file sizes
    predictable and lets operators selectively re-ingest a single month.
  - Archives are append-safe: if a month archive already exists, we append
    (this matters if a previous rotation aborted partway).
  - Deletion happens in batches of 5000 to avoid one giant transaction.
  - Idempotent: running twice in a row produces no extra work the second
    time (no rows match the cutoff).
  - Crash-safe: if we crash mid-rotation, the archive may have duplicates
    but no data is lost. Re-running rotation cleans up.

What rotation does NOT do:
  - VACUUM the live DB (expensive, locks; operators run `aihub vacuum`
    separately when they want to reclaim disk).
  - Delete archives (they're permanent until the operator removes them).
"""
from __future__ import annotations

import dataclasses
import gzip
import io
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class RotationSummary:
    """What happened in one rotation run. Returned to CLI + scheduler."""
    months_examined: list[str] = dataclasses.field(default_factory=list)
    months_archived: list[str] = dataclasses.field(default_factory=list)
    rows_archived: int = 0
    rows_deleted: int = 0
    files_written: list[str] = dataclasses.field(default_factory=list)
    bytes_written: int = 0
    dry_run: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# ---------------------------------------------------------------------------
# Path resolution — archive dir defaults to <db_dir>/archives
# ---------------------------------------------------------------------------
def resolve_archive_dir(audit_archive_dir_setting: str, database_url: str) -> Path:
    """Resolve the archive directory. If the setting is empty, place archives
    next to the platform DB file under `archives/`."""
    if audit_archive_dir_setting:
        return Path(audit_archive_dir_setting)

    # Derive from database_url
    if database_url.startswith("sqlite+aiosqlite:///"):
        db_path = Path(database_url.replace("sqlite+aiosqlite:///", ""))
        return db_path.parent / "archives"
    if database_url.startswith("sqlite:///"):
        db_path = Path(database_url.replace("sqlite:///", ""))
        return db_path.parent / "archives"

    # Postgres or other: caller must set audit_archive_dir explicitly
    raise ValueError(
        "audit_archive_dir must be set explicitly when DATABASE_URL is not SQLite"
    )


# ---------------------------------------------------------------------------
# Month math
# ---------------------------------------------------------------------------
def cutoff_iso_for_retention(months: int, now: datetime | None = None) -> str:
    """Return the first day of the month that's `months` whole months ago,
    in ISO format. Rows with `created_at < cutoff` are eligible for archive.

    Example: now=2026-06-04, months=24 → "2024-06-01T00:00:00"
    """
    now = (now or datetime.now(timezone.utc)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    # Subtract `months` months by walking back month-by-month so we never
    # have to do calendar-arithmetic edge cases (no "subtract 24 months from
    # the 31st" gotchas).
    year, month = now.year, now.month
    for _ in range(months):
        month -= 1
        if month <= 0:
            month = 12
            year -= 1
    cutoff = datetime(year, month, 1, tzinfo=timezone.utc)
    return cutoff.isoformat().replace("+00:00", "")


# ---------------------------------------------------------------------------
# Rotation core
# ---------------------------------------------------------------------------
DELETE_BATCH_SIZE = 5000


async def rotate_audit_logs(
    engine: AsyncEngine,
    archive_dir: Path,
    retention_months: int,
    dry_run: bool = False,
    now: datetime | None = None,
) -> RotationSummary:
    """Archive + delete audit_logs rows older than the retention cutoff.

    Returns a RotationSummary describing what happened (or would have, with
    dry_run=True). Safe to call concurrently with the platform serving traffic
    — WAL mode means we don't block readers, and the DELETE batches keep
    write lock holds small.
    """
    summary = RotationSummary(dry_run=dry_run)

    if retention_months < 1:
        summary.error = "retention_months must be >= 1"
        return summary

    cutoff_iso = cutoff_iso_for_retention(retention_months, now=now)

    archive_dir.mkdir(parents=True, exist_ok=True)

    # Find distinct (year, month) pairs eligible for rotation, oldest first.
    # SQLite's strftime on TEXT dates handles ISO timestamps cleanly.
    async with engine.connect() as conn:
        months_query = text(
            "SELECT strftime('%Y-%m', created_at) AS ym, COUNT(*) AS n "
            "FROM audit_logs "
            "WHERE created_at < :cutoff "
            "GROUP BY ym "
            "ORDER BY ym ASC"
        )
        rows = (await conn.execute(months_query, {"cutoff": cutoff_iso})).all()
        eligible_months = [(r[0], r[1]) for r in rows]

    if not eligible_months:
        logger.info("audit_logs rotation: nothing to do (cutoff=%s)", cutoff_iso)
        return summary

    for ym, expected_n in eligible_months:
        summary.months_examined.append(ym)
        year_str, month_str = ym.split("-")
        archive_path = archive_dir / f"audit_{year_str}_{month_str}.jsonl.gz"

        # 1) Archive — stream rows to the gzipped JSONL file
        if not dry_run:
            archived_n, bytes_written = await _archive_month_to_file(
                engine, year_str, month_str, archive_path
            )
            summary.rows_archived += archived_n
            summary.bytes_written += bytes_written
            summary.files_written.append(str(archive_path))
        else:
            summary.rows_archived += expected_n  # what WOULD be archived

        # 2) Delete in batches
        if not dry_run:
            deleted_n = await _delete_month(engine, year_str, month_str)
            summary.rows_deleted += deleted_n
            summary.months_archived.append(ym)
            logger.info(
                "audit_logs rotation: archived month %s (rows=%d, file=%s)",
                ym, deleted_n, archive_path,
            )
        else:
            summary.rows_deleted += expected_n
            summary.months_archived.append(ym)

    return summary


async def _archive_month_to_file(
    engine: AsyncEngine, year_str: str, month_str: str, archive_path: Path
) -> tuple[int, int]:
    """Stream all audit_logs rows for the given month to a gzipped JSONL file.

    Append mode: if the archive exists, we append (recovery-safe).
    """
    count = 0
    initial_size = archive_path.stat().st_size if archive_path.exists() else 0

    # Open for append-binary; gzip's append mode lets multiple gzip members
    # concatenate, which gunzip handles transparently.
    with gzip.open(archive_path, "ab") as gz:
        async with engine.connect() as conn:
            stream = await conn.stream(
                text(
                    "SELECT id, user_id, action, resource_type, resource_id, "
                    "       details, created_at "
                    "FROM audit_logs "
                    "WHERE strftime('%Y', created_at) = :y "
                    "  AND strftime('%m', created_at) = :m "
                    "ORDER BY created_at, id"
                ),
                {"y": year_str, "m": month_str},
            )
            async for row in stream:
                rec = {
                    "id": row[0],
                    "user_id": row[1],
                    "action": row[2],
                    "resource_type": row[3],
                    "resource_id": row[4],
                    "details": row[5],
                    "created_at": (
                        row[6].isoformat() if hasattr(row[6], "isoformat") else str(row[6])
                    ),
                }
                gz.write((json.dumps(rec, default=str) + "\n").encode("utf-8"))
                count += 1

    final_size = archive_path.stat().st_size
    return count, final_size - initial_size


async def _delete_month(engine: AsyncEngine, year_str: str, month_str: str) -> int:
    """Delete all audit_logs rows for the given month in batches."""
    total = 0
    while True:
        async with engine.begin() as conn:
            res = await conn.execute(
                text(
                    "DELETE FROM audit_logs WHERE id IN ("
                    "  SELECT id FROM audit_logs "
                    "  WHERE strftime('%Y', created_at) = :y "
                    "    AND strftime('%m', created_at) = :m "
                    "  LIMIT :limit"
                    ")"
                ),
                {"y": year_str, "m": month_str, "limit": DELETE_BATCH_SIZE},
            )
            deleted = res.rowcount or 0
            total += deleted
        if deleted < DELETE_BATCH_SIZE:
            break
    return total


# ---------------------------------------------------------------------------
# Status reporting for `aihub doctor`
# ---------------------------------------------------------------------------
async def rotation_status(engine: AsyncEngine, archive_dir: Path) -> dict[str, Any]:
    """Snapshot of rotation state for the doctor + admin UI.

    Returns: { live_rows, live_earliest, live_latest, archive_files,
               archive_bytes, archive_months }
    """
    async with engine.connect() as conn:
        n_row = (await conn.execute(text("SELECT COUNT(*) FROM audit_logs"))).fetchone()
        early_row = (await conn.execute(text(
            "SELECT MIN(created_at) FROM audit_logs"
        ))).fetchone()
        late_row = (await conn.execute(text(
            "SELECT MAX(created_at) FROM audit_logs"
        ))).fetchone()

    archive_files: list[tuple[str, int]] = []
    if archive_dir.exists():
        for p in sorted(archive_dir.glob("audit_*.jsonl.gz")):
            archive_files.append((p.name, p.stat().st_size))

    return {
        "live_rows": int(n_row[0]) if n_row else 0,
        "live_earliest": early_row[0] if early_row else None,
        "live_latest": late_row[0] if late_row else None,
        "archive_files": archive_files,
        "archive_total_bytes": sum(s for _, s in archive_files),
        "archive_months_archived": len(archive_files),
        "archive_dir": str(archive_dir),
    }


# ---------------------------------------------------------------------------
# Background scheduler — runs forever in the FastAPI lifespan
# ---------------------------------------------------------------------------
async def audit_rotation_loop():
    """Daily-ish loop that calls rotate_audit_logs based on settings.

    Started by main.py's lifespan; cancelled on shutdown. All exceptions are
    swallowed so a one-off rotation failure doesn't kill the loop forever.
    """
    import asyncio

    from .config import settings
    from .database import engine

    if not settings.audit_rotation_enabled:
        logger.info("audit_rotation: disabled via settings; loop not started")
        return

    # First run delayed by a short slice so we don't pile work on startup;
    # subsequent runs are spaced by audit_rotation_interval_hours.
    await asyncio.sleep(60)

    while True:
        try:
            archive_dir = resolve_archive_dir(
                settings.audit_archive_dir, settings.database_url
            )
            summary = await rotate_audit_logs(
                engine,
                archive_dir=archive_dir,
                retention_months=settings.audit_retention_months,
            )
            if summary.months_archived:
                logger.info(
                    "audit_logs rotation complete: months=%s rows=%d bytes=%d",
                    summary.months_archived, summary.rows_deleted,
                    summary.bytes_written,
                )
        except Exception:
            logger.exception("audit_rotation: loop iteration failed")

        await asyncio.sleep(settings.audit_rotation_interval_hours * 3600)
