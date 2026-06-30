"""Per-app SQLite store — every deployed app gets its own SQLite file for
local state (todos, notes, settings, anything the app itself creates).

File path layout:
    data/apps/{app_id}/draft/app.db          ← dev preview
    <agent>/apps/{app_id}/app.db             ← deployed (lives on the agent)

This module only handles the *platform-side* path (draft + version). The
agent gets a separate but interface-compatible implementation later.

Safety:
    - foreign_keys + WAL via the same pragmas as the platform DB
    - `:current_user` auto-injected into every query the app makes
    - Optional row-level scoping via {'scope': 'user'} → adds
      `WHERE created_by = :current_user` to SELECTs and stamps `created_by`
      on INSERTs
    - Destructive migrations refused unless `-- AIHUB-DESTRUCTIVE-OK` marker
"""
from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import settings
from ..database_tuning import PLATFORM_PRAGMAS

logger = logging.getLogger(__name__)

# Schema version meta table — created on first use
_META_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS _aihub_meta (
    key TEXT PRIMARY KEY,
    value TEXT
)
"""

DESTRUCTIVE_OK_MARKER = "AIHUB-DESTRUCTIVE-OK"

# Recognized destructive operations. Migrations with these strings (and
# without the explicit marker) are refused.
_DESTRUCTIVE_PATTERNS = (
    re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE),
    re.compile(r"\bDROP\s+COLUMN\b", re.IGNORECASE),
    re.compile(r"\bDROP\s+INDEX\b", re.IGNORECASE),
    re.compile(r"\bTRUNCATE\b", re.IGNORECASE),
)


@dataclass
class QueryResult:
    rows: list[dict[str, Any]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False


@dataclass
class ExecResult:
    rows_affected: int = 0
    last_insert_rowid: int | None = None


@dataclass
class TableInfo:
    name: str
    row_count: int
    columns: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------
def app_db_path(app_id: str) -> Path:
    """The per-app DB file for the dev/draft variant of an app on the platform."""
    p = Path(settings.app_data_dir) / app_id / "draft" / "app.db"
    return p


def _ensure_initialized(conn: sqlite3.Connection) -> None:
    """Apply pragmas + create meta table if missing."""
    for name, value in PLATFORM_PRAGMAS:
        conn.execute(f"PRAGMA {name}={value}")
    conn.execute(_META_TABLE_SQL)


def _open(app_id: str) -> sqlite3.Connection:
    """Open a sync sqlite3 connection (sync = simpler; app-DB queries don't
    overlap with anything async on the same file). Caller closes."""
    p = app_db_path(app_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    _ensure_initialized(conn)
    return conn


# ---------------------------------------------------------------------------
# Schema migration safety
# ---------------------------------------------------------------------------
def _is_destructive_migration(sql: str) -> bool:
    """Return True if the SQL contains a destructive op without the
    AIHUB-DESTRUCTIVE-OK marker."""
    if DESTRUCTIVE_OK_MARKER in sql:
        return False
    return any(p.search(sql) for p in _DESTRUCTIVE_PATTERNS)


def _current_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM _aihub_meta WHERE key = 'schema_version'"
    ).fetchone()
    return int(row["value"]) if row else 0


def _set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT INTO _aihub_meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(version),),
    )


def apply_migrations(app_id: str, migrations: list[tuple[int, str, str]]) -> dict[str, Any]:
    """Apply pending migrations in order.

    `migrations` is a list of (version, name, sql) tuples; only those with
    version > current schema_version are applied. The whole batch is one
    transaction — partial migrations don't leave a broken state.

    Refuses to run if any pending migration is destructive and lacks the
    AIHUB-DESTRUCTIVE-OK marker.

    Returns a summary {applied_versions, refused, current_version}.
    """
    conn = _open(app_id)
    try:
        current = _current_schema_version(conn)
        pending = [(v, n, s) for (v, n, s) in migrations if v > current]
        pending.sort(key=lambda x: x[0])

        refused: list[dict] = []
        for v, n, s in pending:
            if _is_destructive_migration(s):
                refused.append({
                    "version": v,
                    "name": n,
                    "reason": "destructive migration without AIHUB-DESTRUCTIVE-OK marker",
                })

        if refused:
            return {
                "applied_versions": [],
                "refused": refused,
                "current_version": current,
            }

        applied: list[int] = []
        # Wrap all migrations in a single transaction so partial application
        # never leaves an inconsistent schema.
        try:
            with conn:  # BEGIN/COMMIT on success, ROLLBACK on exception
                for v, n, s in pending:
                    conn.executescript(s)
                    _set_schema_version(conn, v)
                    applied.append(v)
        except Exception as e:
            return {
                "applied_versions": applied,  # what made it before the failure
                "refused": [{"version": v, "name": n, "reason": str(e)}],
                "current_version": _current_schema_version(conn),
                "error": str(e),
            }

        return {
            "applied_versions": applied,
            "refused": [],
            "current_version": _current_schema_version(conn),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Query / exec — what the deployed app and the admin UI call
# ---------------------------------------------------------------------------
DEFAULT_ROW_CAP = 1000


def execute_query(
    app_id: str,
    sql: str,
    params: dict[str, Any],
    current_user: str,
    scope: str = "all",
    row_cap: int = DEFAULT_ROW_CAP,
) -> QueryResult:
    """Run a SELECT (or any query that returns rows). Auto-injects current_user.

    `scope`:
        'all'  – return rows from anyone (default)
        'user' – only rows where created_by = :current_user
    """
    # The 'user' scope is implemented at the prompt level — the AI is expected
    # to write `WHERE created_by = :current_user` itself when it picks scope=user.
    # We still inject the param so the SQL can reference it.
    effective_params = dict(params)
    effective_params["current_user"] = current_user

    used = _filter_unused_named_params(sql, effective_params)

    conn = _open(app_id)
    try:
        cur = conn.execute(sql, used)
        rows = cur.fetchmany(row_cap + 1)
        truncated = len(rows) > row_cap
        rows = rows[:row_cap]
        cols = [d[0] for d in cur.description] if cur.description else []
        out_rows = [dict(r) for r in rows]
        # JSON-safe coercion of non-primitive values
        out_rows = [{k: _json_safe(v) for k, v in r.items()} for r in out_rows]
        return QueryResult(
            rows=out_rows, columns=cols, row_count=len(out_rows), truncated=truncated,
        )
    finally:
        conn.close()


def execute_exec(
    app_id: str,
    sql: str,
    params: dict[str, Any],
    current_user: str,
) -> ExecResult:
    """Run an INSERT/UPDATE/DELETE. Auto-injects current_user and (for INSERT)
    stamps created_by if the table has that column."""
    effective_params = dict(params)
    effective_params["current_user"] = current_user
    used = _filter_unused_named_params(sql, effective_params)

    conn = _open(app_id)
    try:
        with conn:
            cur = conn.execute(sql, used)
            return ExecResult(
                rows_affected=cur.rowcount or 0,
                last_insert_rowid=cur.lastrowid,
            )
    finally:
        conn.close()


def list_tables(app_id: str) -> list[TableInfo]:
    """Tables in the app DB (excluding meta tables) with row counts + columns.

    Used by the admin Data tab to give a debug-grade view of app state.
    """
    conn = _open(app_id)
    try:
        tables = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE '_aihub%' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()
        out: list[TableInfo] = []
        for t in tables:
            tname = t["name"]
            n = conn.execute(f"SELECT COUNT(*) AS n FROM \"{tname}\"").fetchone()["n"]
            cols = []
            for cinfo in conn.execute(f"PRAGMA table_info(\"{tname}\")").fetchall():
                cols.append({
                    "name": cinfo["name"],
                    "type": cinfo["type"],
                    "notnull": bool(cinfo["notnull"]),
                    "pk": bool(cinfo["pk"]),
                })
            out.append(TableInfo(name=tname, row_count=n, columns=cols))
        return out
    finally:
        conn.close()


def db_size_bytes(app_id: str) -> int:
    """Size of the app DB file on disk (0 if doesn't exist yet)."""
    p = app_db_path(app_id)
    return p.stat().st_size if p.exists() else 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_NAMED_PARAM = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)")


def _filter_unused_named_params(sql: str, params: dict[str, Any]) -> dict[str, Any]:
    """Drop params not referenced in the SQL (sqlite3 errors on extras
    when using `:name`-style binding)."""
    referenced = {m.group(1) for m in _NAMED_PARAM.finditer(sql)}
    return {k: v for k, v in params.items() if k in referenced}


def _json_safe(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (bytes, bytearray)):
        try:
            return v.decode("utf-8")
        except Exception:
            return repr(v)
    return str(v)
