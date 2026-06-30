"""SQLite pragma tuning + composite index migrations for the platform DB.

The defaults SQLAlchemy gives us aren't well-suited to a real workload (default
journal mode is DELETE, which blocks readers on every write). This module
applies a vetted set of pragmas on every new connection — runs once per
process, idempotent at the DB level.

The pragmas chosen here are the well-known "SQLite for production" set:

  journal_mode = WAL          readers + writer in parallel; file-level setting,
                              persists once applied
  synchronous = NORMAL        durable across power loss in WAL mode (don't
                              confuse with the DELETE-mode meaning)
  busy_timeout = 5000         retry transient locks for up to 5s instead of
                              raising "database is locked"
  cache_size = -64000         64 MB page cache per connection
  temp_store = MEMORY         temp tables / indexes never hit disk
  foreign_keys = ON           enforce FK constraints (off by default in sqlite!)

These take effect on every new DBAPI connection (which is what `connect`
event listeners on `engine.sync_engine` give us, even for an async engine).
"""
from __future__ import annotations

import logging

from sqlalchemy import event

logger = logging.getLogger(__name__)

PLATFORM_PRAGMAS: tuple[tuple[str, str], ...] = (
    ("journal_mode", "WAL"),
    ("synchronous", "NORMAL"),
    ("busy_timeout", "5000"),
    ("cache_size", "-64000"),    # 64 MB; negative means KB
    ("temp_store", "MEMORY"),
    ("foreign_keys", "ON"),
)


def apply_sqlite_tuning(engine) -> None:
    """Attach a connect listener that applies our pragma set on every new DBAPI
    connection. Safe to call once at engine creation; no-op for non-sqlite engines.

    Why per-connection: SQLite stores some pragmas (`journal_mode`, `foreign_keys`)
    per-connection rather than per-database. We can't set them once and forget;
    every new connection needs the same treatment.
    """
    if not engine.url.drivername.startswith("sqlite"):
        return

    @event.listens_for(engine.sync_engine, "connect")
    def _apply_pragmas(dbapi_conn, _):
        # For aiosqlite, dbapi_conn is the SQLAlchemy async-adapter wrapping the
        # real aiosqlite.Connection (which itself wraps a sync sqlite3.Connection).
        # cursor().execute() works through the wrapper.
        try:
            cur = dbapi_conn.cursor()
        except Exception as e:
            logger.warning("Could not get cursor for pragma tuning: %s", e)
            return
        try:
            for name, value in PLATFORM_PRAGMAS:
                cur.execute(f"PRAGMA {name}={value}")
        except Exception as e:
            logger.warning("Pragma tuning failed: %s", e)
        finally:
            try:
                cur.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Composite indexes that pay for themselves at the query patterns we actually
# run. CREATE INDEX IF NOT EXISTS is idempotent — safe to run every startup.
# ---------------------------------------------------------------------------
#
# Format: (index_name, table, columns_clause)
#
# These cover the hot queries:
#   - "recent calls for this dataset"      → audit_logs(resource_id, action, created_at)
#   - "what did this user do"              → audit_logs(user_id, created_at)
#   - "messages in this conversation"      → messages(conversation_id, created_at)
#   - "datasets bound to this app"         → app_dataset_bindings(app_id)
#   - "audit by created_at for retention"  → audit_logs(created_at)
COMPOSITE_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("ix_audit_logs_resource_action_time", "audit_logs",
     "(resource_id, action, created_at)"),
    ("ix_audit_logs_user_time", "audit_logs",
     "(user_id, created_at)"),
    ("ix_audit_logs_created_at", "audit_logs",
     "(created_at)"),
    ("ix_messages_convo_time", "messages",
     "(conversation_id, created_at)"),
    ("ix_app_dataset_bindings_app", "app_dataset_bindings",
     "(app_id)"),
    ("ix_app_dataset_bindings_dataset", "app_dataset_bindings",
     "(dataset_id)"),
)


async def apply_index_migrations(async_engine) -> None:
    """Create composite indexes if missing. Idempotent — safe at every startup.

    Uses CREATE INDEX IF NOT EXISTS rather than scanning sqlite_master first
    because the latter races under concurrent test runs.
    """
    from sqlalchemy import text

    async with async_engine.begin() as conn:
        for name, table, cols in COMPOSITE_INDEXES:
            try:
                await conn.execute(text(
                    f"CREATE INDEX IF NOT EXISTS {name} ON {table}{cols}"
                ))
            except Exception as e:
                # Most common reason: the table doesn't exist yet (very fresh
                # install, models haven't been created). create_all runs before
                # us, so this is unusual — log and continue.
                logger.warning("Index %s on %s%s failed: %s", name, table, cols, e)


# ---------------------------------------------------------------------------
# Read-back helpers — used by `aihub doctor` and tests to verify the pragmas
# actually stuck.
# ---------------------------------------------------------------------------
async def read_pragmas(async_engine) -> dict[str, str]:
    """Return the current pragma values on a fresh connection. Used by the
    doctor CLI + the regression tests."""
    from sqlalchemy import text

    out: dict[str, str] = {}
    async with async_engine.connect() as c:
        for name, _ in PLATFORM_PRAGMAS:
            try:
                r = (await c.execute(text(f"PRAGMA {name}"))).fetchone()
                out[name] = str(r[0]) if r is not None else ""
            except Exception:
                out[name] = "(error)"
    return out


async def list_indexes(async_engine) -> set[str]:
    """Return the set of index names that currently exist in sqlite_master."""
    from sqlalchemy import text

    async with async_engine.connect() as c:
        rows = (await c.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ))).fetchall()
    return {r[0] for r in rows}
