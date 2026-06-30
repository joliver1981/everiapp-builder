"""Generic, database-agnostic test helpers shared across platform integration tests.

These use only SQLite + the ORM — no MSSQL/pyodbc dependency — so they import
cleanly in any environment. (The AIRDB-specific MSSQL fixtures live in a separate
local-only module that depends on a private warehouse and is not part of this repo.)
"""
from __future__ import annotations

import os


def _current_platform_db_path() -> str:
    """Resolve the platform's actual sqlite path at *call time*.

    Tests can't rely on a module-level _DB constant because pytest imports
    every test file before running any test, so the LAST-imported file's
    DATABASE_URL env var wins. Reading from `settings.database_url` here
    means we always hit the DB the platform itself is using.
    """
    from src.config import settings
    url = settings.database_url
    # sqlite+aiosqlite:////absolute/path → strip the prefix to get the path.
    prefix = "sqlite+aiosqlite:///"
    if url.startswith(prefix):
        return url[len(prefix):]
    raise RuntimeError(f"unexpected DATABASE_URL shape: {url}")


async def fetch_admin_user_id_async(session) -> str:
    """Async variant of _resolve_creator_user_id — for test seed functions
    that operate inside `async with async_session() as s:` blocks.

    Tests log in as admin before the seed runs, so the user is present by then.
    """
    from sqlalchemy import select
    from src.auth.models import User

    r = await session.execute(select(User).where(User.username == "admin"))
    user = r.scalar_one_or_none()
    if user:
        return user.id
    r = await session.execute(select(User).limit(1))
    any_user = r.scalar_one_or_none()
    if any_user:
        return any_user.id
    raise RuntimeError(
        "no users in DB — log in via /api/auth/login before seeding apps"
    )


def _resolve_creator_user_id(conn) -> str:
    """Return a real user id from the users table — needed now that
    apps.created_by has FK enforcement (we enabled foreign_keys=ON globally).

    Tests log in as 'admin' before any helper runs, so the admin user already
    exists. Fall back to ANY user row if 'admin' somehow isn't there yet.
    """
    row = conn.execute(
        "SELECT id FROM users WHERE username = 'admin' LIMIT 1"
    ).fetchone()
    if row:
        return row[0]
    row = conn.execute("SELECT id FROM users LIMIT 1").fetchone()
    if row:
        return row[0]
    raise RuntimeError(
        "no user rows exist yet — log in via /api/auth/login before calling "
        "insert_app_and_binding (admin user is lazy-created on first login)"
    )


def insert_app_and_binding(db_path: str | None, app_id: str, dataset_id: str | None) -> None:
    """Direct DB insert to skip the apps service's filesystem scaffolding.

    Pass `None` for `db_path` to resolve from the live settings (works correctly
    across test files). Pass an explicit path if you need to target a specific
    DB (rarely the right call). Idempotent — INSERT OR IGNORE on both rows.
    """
    import sqlite3

    if db_path is None:
        db_path = _current_platform_db_path()

    conn = sqlite3.connect(db_path)
    creator_id = _resolve_creator_user_id(conn)
    conn.execute(
        "INSERT OR IGNORE INTO apps (id, name, description, icon, status, current_version, "
        "ai_toggle_enabled, bug_widget_enabled, bug_fix_auto_approve_max_risk, "
        "ai_verify_level, ai_verify_max_iterations, created_by, created_at, updated_at) "
        "VALUES (?, ?, '', 'app-window', 'draft', 0, 0, 0, 'none', 'tsc_build_boot', 8, ?, "
        "datetime('now'), datetime('now'))",
        (app_id, f"app-{app_id[:8]}", creator_id),
    )
    if dataset_id:
        conn.execute(
            "INSERT OR IGNORE INTO app_dataset_bindings (app_id, dataset_id, created_at) "
            "VALUES (?, ?, datetime('now'))",
            (app_id, dataset_id),
        )
    conn.commit()
    conn.close()


def make_tmp_test_env(name: str) -> str:
    """Set up env vars + a clean temp DB for a test file, return the DB path.

    Call this once per file at module import time, BEFORE importing src.main.
    """
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.gettempdir()) / "aihub-integration"
    tmp.mkdir(parents=True, exist_ok=True)
    db = tmp / f"{name}.db"
    if db.exists():
        try:
            db.unlink()
        except OSError:
            pass

    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db}"
    os.environ["APP_DATA_DIR"] = str(tmp / f"apps_{name}")
    os.environ["DEBUG"] = "true"
    os.environ.setdefault(
        "MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8="
    )
    os.environ.setdefault("JWT_SECRET_KEY", "test-secret")
    return str(db)
