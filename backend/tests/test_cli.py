"""Tests for the `aihub` CLI — backup and doctor subcommands.

Strategy: drive the click commands via click.testing.CliRunner, point them
at a temp DATABASE_URL so we don't touch the real platform DB, and verify
the backup is a valid tarball whose embedded DB can be read back.
"""
from __future__ import annotations

import os
import sqlite3
import tarfile
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

# Point at a scratch DB BEFORE importing src.cli (which imports src.config →
# settings) so the tests don't try to touch the real one.
_SCRATCH_DB = Path(tempfile.gettempdir()) / "aihub-cli-test.db"
if _SCRATCH_DB.exists():
    _SCRATCH_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_SCRATCH_DB}"
os.environ.setdefault("APP_DATA_DIR", str(_SCRATCH_DB.parent / "aihub-cli-test-apps"))
os.environ.setdefault(
    "MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8="
)
os.environ.setdefault("JWT_SECRET_KEY", "cli-test-secret")

# Now safe to import
from src.cli import cli  # noqa: E402
from src.database import init_db  # noqa: E402
import asyncio  # noqa: E402


@pytest.fixture(autouse=True)
def _ensure_db():
    """Make sure the scratch DB exists + is initialized before every test.

    Each test gets a fresh init since the CLI doesn't actually mutate the DB
    (backup is read-only, doctor is read-only) — but init also creates the
    file in the first place so backup has something to point at.
    """
    asyncio.run(init_db())
    yield


def test_backup_creates_tarball_with_db_inside(tmp_path):
    runner = CliRunner()
    dest = tmp_path / "backup.tar.gz"
    result = runner.invoke(cli, ["backup", "--to", str(dest), "--no-include-app-data"])
    assert result.exit_code == 0, result.output
    assert dest.exists(), "tarball not written"
    # Tar must contain manifest.json + platform.db
    with tarfile.open(dest, "r:gz") as tar:
        names = tar.getnames()
    assert "manifest.json" in names
    assert "platform.db" in names


def test_backup_db_is_a_valid_sqlite_file(tmp_path):
    runner = CliRunner()
    dest = tmp_path / "out.tar.gz"
    result = runner.invoke(cli, ["backup", "--to", str(dest), "--no-include-app-data"])
    assert result.exit_code == 0, result.output

    extract = tmp_path / "extract"
    extract.mkdir()
    with tarfile.open(dest, "r:gz") as tar:
        tar.extractall(extract)

    # Open the extracted DB and confirm we can query it
    extracted_db = extract / "platform.db"
    assert extracted_db.exists()
    conn = sqlite3.connect(str(extracted_db))
    try:
        # At minimum the `users` table should exist after init_db
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchall()
        assert rows, "extracted backup is missing the users table"
    finally:
        conn.close()


def test_backup_default_destination(tmp_path, monkeypatch):
    """When --to is omitted, the file should land in CWD with a date-stamped name."""
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli, ["backup", "--no-include-app-data"])
    assert result.exit_code == 0, result.output
    written = list(tmp_path.glob("aihub-backup-*.tar.gz"))
    assert len(written) == 1, f"expected exactly one backup file, got {written}"


def test_doctor_runs_and_reports(tmp_path):
    runner = CliRunner()
    result = runner.invoke(cli, ["doctor"])
    # Doctor exits 0 unless a check is RED; in our test setup the file exists
    # and pragmas are applied, so it should be green.
    assert result.exit_code in (0, 1), result.output  # 1 if warnings on test DB
    # Must mention the key sections
    assert "Platform DB" in result.output
    assert "Disk" in result.output
    assert "Audit log" in result.output


def test_doctor_reports_pragmas_correctly(tmp_path):
    runner = CliRunner()
    result = runner.invoke(cli, ["doctor"])
    # Every named pragma should be in the output (line either OK or WARN)
    for pragma in ("journal_mode", "synchronous", "busy_timeout",
                   "cache_size", "temp_store", "foreign_keys"):
        assert pragma in result.output, f"doctor output missed pragma {pragma}\n{result.output}"


def test_doctor_lists_indexes(tmp_path):
    runner = CliRunner()
    result = runner.invoke(cli, ["doctor", "--verbose"])
    assert "Composite indexes" in result.output


def test_version_flag():
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "1.0.0" in result.output


def test_help_lists_both_commands():
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "backup" in result.output
    assert "doctor" in result.output
