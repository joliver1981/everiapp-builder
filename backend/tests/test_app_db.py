"""Tests for the per-app SQLite store backend."""
from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

import pytest


@pytest.fixture
def app_db_root(tmp_path, monkeypatch):
    """Point app_data_dir at a tmp dir for THIS TEST ONLY.

    Critically: we patch the live `settings.app_data_dir` attribute (rather
    than the env var + reloading the module). Module reloads poisoned
    src.config.settings for every subsequent test in the session — discovered
    by running the full gate after this fixture was added.
    """
    from src.config import settings
    monkeypatch.setattr(settings, "app_data_dir", str(tmp_path))
    return tmp_path


def test_open_creates_file_and_meta_table(app_db_root):
    from src.app_db.service import _open
    app_id = str(uuid.uuid4())
    conn = _open(app_id)
    try:
        # _aihub_meta should exist
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='_aihub_meta'"
        ).fetchone()
        assert row is not None
    finally:
        conn.close()


def test_apply_migrations_runs_and_tracks_version(app_db_root):
    from src.app_db.service import apply_migrations
    app_id = str(uuid.uuid4())
    result = apply_migrations(app_id, [
        (1, "init", "CREATE TABLE todos (id INTEGER PRIMARY KEY, title TEXT)"),
        (2, "add_done", "ALTER TABLE todos ADD COLUMN done BOOLEAN DEFAULT 0"),
    ])
    assert result["applied_versions"] == [1, 2]
    assert result["current_version"] == 2
    assert result["refused"] == []

    # Second run: idempotent
    result2 = apply_migrations(app_id, [
        (1, "init", "CREATE TABLE todos (id INTEGER PRIMARY KEY)"),
        (2, "add_done", "ALTER TABLE todos ADD COLUMN done BOOLEAN"),
    ])
    assert result2["applied_versions"] == []


def test_destructive_migration_refused_without_marker(app_db_root):
    from src.app_db.service import apply_migrations
    app_id = str(uuid.uuid4())
    apply_migrations(app_id, [(1, "init", "CREATE TABLE x (id INT)")])

    bad = apply_migrations(app_id, [
        (2, "danger", "DROP TABLE x"),  # no AIHUB-DESTRUCTIVE-OK marker
    ])
    assert bad["applied_versions"] == []
    assert any("destructive" in r["reason"].lower() for r in bad["refused"])


def test_destructive_migration_allowed_with_marker(app_db_root):
    from src.app_db.service import apply_migrations
    app_id = str(uuid.uuid4())
    apply_migrations(app_id, [(1, "init", "CREATE TABLE x (id INT)")])
    ok = apply_migrations(app_id, [
        (2, "danger", "-- AIHUB-DESTRUCTIVE-OK\nDROP TABLE x"),
    ])
    assert ok["applied_versions"] == [2]


def test_query_and_exec_round_trip(app_db_root):
    from src.app_db.service import apply_migrations, execute_exec, execute_query
    app_id = str(uuid.uuid4())
    apply_migrations(app_id, [(1, "init",
        "CREATE TABLE todos (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "title TEXT NOT NULL, done BOOLEAN DEFAULT 0, created_by TEXT)"
    )])

    # Insert
    r1 = execute_exec(
        app_id,
        "INSERT INTO todos (title, created_by) VALUES (:t, :current_user)",
        {"t": "Ship it"},
        current_user="alice",
    )
    assert r1.rows_affected == 1
    assert r1.last_insert_rowid == 1

    # Read
    q = execute_query(
        app_id, "SELECT id, title, created_by FROM todos", {},
        current_user="alice",
    )
    assert q.row_count == 1
    assert q.rows[0]["title"] == "Ship it"
    assert q.rows[0]["created_by"] == "alice"


def test_current_user_injected_in_query(app_db_root):
    """A query that uses :current_user gets it bound."""
    from src.app_db.service import apply_migrations, execute_query
    app_id = str(uuid.uuid4())
    apply_migrations(app_id, [(1, "init", "CREATE TABLE x (id INT)")])
    q = execute_query(app_id, "SELECT :current_user AS who", {}, current_user="bob")
    assert q.rows[0]["who"] == "bob"


def test_row_cap_truncation(app_db_root):
    from src.app_db.service import apply_migrations, execute_exec, execute_query
    app_id = str(uuid.uuid4())
    apply_migrations(app_id, [(1, "init", "CREATE TABLE big (id INTEGER)")])
    for i in range(50):
        execute_exec(app_id, "INSERT INTO big VALUES (:i)", {"i": i}, current_user="x")
    q = execute_query(app_id, "SELECT id FROM big ORDER BY id", {}, current_user="x", row_cap=20)
    assert q.row_count == 20
    assert q.truncated


def test_list_tables_and_row_counts(app_db_root):
    from src.app_db.service import apply_migrations, execute_exec, list_tables
    app_id = str(uuid.uuid4())
    apply_migrations(app_id, [(1, "init",
        "CREATE TABLE alpha (id INT); CREATE TABLE beta (id INT)"
    )])
    execute_exec(app_id, "INSERT INTO alpha VALUES (1)", {}, current_user="x")
    execute_exec(app_id, "INSERT INTO alpha VALUES (2)", {}, current_user="x")
    execute_exec(app_id, "INSERT INTO beta VALUES (1)", {}, current_user="x")

    tables = list_tables(app_id)
    by_name = {t.name: t for t in tables}
    assert "alpha" in by_name and "beta" in by_name
    # _aihub_meta is filtered out
    assert "_aihub_meta" not in by_name
    assert by_name["alpha"].row_count == 2
    assert by_name["beta"].row_count == 1
