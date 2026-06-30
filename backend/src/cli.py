"""aihub CLI — `aihub backup` and `aihub doctor` for on-prem operators.

The installer wires this up as a console script via pyproject.toml so IT can
run plain `aihub backup` after install. Until the installer ships, run it as:

    cd backend && ../.venv/Scripts/python.exe -m src.cli doctor
    cd backend && ../.venv/Scripts/python.exe -m src.cli backup --to <path>

Design goals:
  - Single binary, no daemon-style state — talk directly to the DB file
  - Online backup (the platform doesn't have to stop)
  - Plain-text output that IT can grep / log
  - Exit code 0 on green, 1 on any error/warning
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click


# ---------------------------------------------------------------------------
# Path helpers — derive the platform paths from settings (or env), without
# pulling in the whole FastAPI app.
# ---------------------------------------------------------------------------
def _platform_db_path() -> Path:
    """Resolve the platform DB file path from the active config."""
    from .config import settings

    url = settings.database_url
    # We only handle sqlite here (Postgres support is wave 4).
    prefix = "sqlite+aiosqlite:///"
    if url.startswith(prefix):
        return Path(url[len(prefix):]).resolve()
    if url.startswith("sqlite:///"):
        return Path(url[len("sqlite:///"):]).resolve()
    raise click.ClickException(
        f"database URL is not a SQLite path: {url}\n"
        "aihub backup currently only supports SQLite. Postgres support is on the roadmap."
    )


def _data_dir() -> Path:
    from .config import settings
    return Path(settings.app_data_dir).resolve()


def _archives_dir() -> Path:
    # Co-located with the platform DB so backups travel with the data set.
    return _platform_db_path().parent / "archives"


# ---------------------------------------------------------------------------
# Color / output — kept ASCII-safe for Windows consoles.
# ---------------------------------------------------------------------------
def _ok(msg: str) -> None:
    click.secho(f"[OK]   {msg}", fg="green")


def _warn(msg: str) -> None:
    click.secho(f"[WARN] {msg}", fg="yellow")


def _err(msg: str) -> None:
    click.secho(f"[ERR]  {msg}", fg="red")


def _info(msg: str) -> None:
    click.echo(f"       {msg}")


def _human_bytes(n: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.1f} {u}"
        f /= 1024
    return f"{n} B"


# ---------------------------------------------------------------------------
# aihub backup
# ---------------------------------------------------------------------------
@click.command(name="backup")
@click.option(
    "--to", "to_path",
    type=click.Path(dir_okay=True, file_okay=True, resolve_path=True),
    default=None,
    help="Destination directory OR full .tar.gz path. Default: ./aihub-backup-<TS>.tar.gz",
)
@click.option(
    "--include-app-data/--no-include-app-data",
    default=True,
    help="Include data/apps/ (deployed app file snapshots). Default: yes.",
)
@click.option(
    "--include-archives/--no-include-archives",
    default=False,
    help="Include rolled audit_log archives. Default: no (they're already cold).",
)
def backup(to_path: str | None, include_app_data: bool, include_archives: bool) -> None:
    """Take a live, consistent backup of the platform DB and surrounding state.

    Uses SQLite's online backup API so the platform doesn't need to be stopped.
    Output is a tarball: platform_db + (optionally) data/apps + manifest.
    """
    src_db = _platform_db_path()
    if not src_db.exists():
        _err(f"platform DB not found at {src_db}")
        sys.exit(1)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    # Resolve destination
    if to_path is None:
        dest = Path.cwd() / f"aihub-backup-{ts}.tar.gz"
    else:
        dest = Path(to_path)
        if dest.is_dir() or dest.suffix == "":
            dest = dest / f"aihub-backup-{ts}.tar.gz"

    dest.parent.mkdir(parents=True, exist_ok=True)

    # Stage the snapshot in a temp dir so we can build a tar deterministically
    staging = Path(tempfile.mkdtemp(prefix="aihub-backup-"))
    try:
        # 1) Snapshot the DB via the online backup API (safe while the platform
        #    is being written to)
        click.echo(f"Backing up platform DB:  {src_db}")
        db_snapshot = staging / "platform.db"
        _online_backup_sqlite(src_db, db_snapshot)
        _ok(f"DB snapshot: {_human_bytes(db_snapshot.stat().st_size)}")

        # 2) Manifest
        manifest = {
            "schema_version": 1,
            "taken_at": ts,
            "platform_db_path": str(src_db),
            "app_data_dir": str(_data_dir()),
            "include_app_data": include_app_data,
            "include_archives": include_archives,
            "tool_version": "aihub-cli/1.0",
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2))

        # 3) Build the tar
        click.echo(f"Writing tarball:         {dest}")
        with tarfile.open(dest, "w:gz") as tar:
            tar.add(staging / "manifest.json", arcname="manifest.json")
            tar.add(db_snapshot, arcname="platform.db")
            if include_app_data and _data_dir().exists():
                # data/apps/ contains both 'apps' subdir and 'aihub.db' (which is the
                # DB itself — already snapshotted). Walk and include only files.
                apps_dir = _data_dir() / "apps"
                if apps_dir.exists():
                    click.echo("Adding data/apps tree...")
                    tar.add(apps_dir, arcname="data/apps")
            if include_archives and _archives_dir().exists():
                click.echo("Adding archives...")
                tar.add(_archives_dir(), arcname="data/archives")

        size = dest.stat().st_size
        _ok(f"Backup complete: {dest} ({_human_bytes(size)})")
        click.echo("")
        click.echo("To restore on another machine:")
        click.echo(f"  1. Stop the AIHub service")
        click.echo(f"  2. tar -xzf {dest.name} -C <restore_dir>")
        click.echo(f"  3. Move platform.db over data/aihub.db (or wherever DATABASE_URL points)")
        click.echo(f"  4. Restore data/apps/ if included")
        click.echo(f"  5. Start the AIHub service")
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _online_backup_sqlite(src: Path, dest: Path) -> None:
    """SQLite online backup. Safe to run while the source DB is being written
    to — uses BACKUP API, not just `cp`."""
    src_conn = sqlite3.connect(str(src))
    dest_conn = sqlite3.connect(str(dest))
    try:
        # progress=None means "do it all in one shot"
        src_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        src_conn.close()


# ---------------------------------------------------------------------------
# aihub doctor
# ---------------------------------------------------------------------------
@click.command(name="doctor")
@click.option("--verbose", "-v", is_flag=True, help="Print extra detail.")
def doctor(verbose: bool) -> None:
    """Run a panel of health checks against the local install.

    Exits non-zero if any check is RED. WARN-only runs return 0 but print a
    yellow summary so monitoring can scrape `aihub doctor --verbose`.
    """
    from .database import engine
    from .database_tuning import PLATFORM_PRAGMAS, COMPOSITE_INDEXES, list_indexes, read_pragmas

    errors = 0
    warnings = 0

    click.echo("=" * 60)
    click.echo("aihub doctor")
    click.echo("=" * 60)
    click.echo("")

    # ----- Platform DB ---------------------------------------------------
    click.echo("Platform DB")
    click.echo("-" * 60)
    db_path = _platform_db_path()
    if not db_path.exists():
        _err(f"Platform DB not found: {db_path}")
        errors += 1
    else:
        _ok(f"DB file: {db_path}")
        _info(f"size: {_human_bytes(db_path.stat().st_size)}")

        # Also check WAL sidecar
        wal_path = db_path.with_suffix(db_path.suffix + "-wal")
        if wal_path.exists():
            _ok(f"WAL sidecar present: {_human_bytes(wal_path.stat().st_size)}")
        else:
            _warn(f"WAL sidecar missing at {wal_path} — WAL mode may not be active yet")
            warnings += 1

    # Read back live pragma values
    async def _read_pragmas_and_indexes():
        return await read_pragmas(engine), await list_indexes(engine)

    try:
        pragmas, indexes = asyncio.run(_read_pragmas_and_indexes())
    except Exception as e:
        _err(f"Could not read DB state: {e}")
        sys.exit(1)

    # Pragma assertions — each pragma has an expected value mapped from
    # PLATFORM_PRAGMAS. SQLite normalizes some values (e.g. "WAL" → "wal").
    pragma_expectations = {
        "journal_mode": ("wal",),
        "synchronous": ("1", "normal"),   # NORMAL = 1 in SQLite
        "busy_timeout": ("5000",),
        "cache_size": ("-64000",),
        "temp_store": ("2", "memory"),    # MEMORY = 2 in SQLite
        "foreign_keys": ("1", "on"),
    }
    for name, _ in PLATFORM_PRAGMAS:
        actual = (pragmas.get(name) or "").lower()
        expected = pragma_expectations.get(name, ())
        if actual in expected:
            _ok(f"PRAGMA {name} = {actual}")
        else:
            _warn(f"PRAGMA {name} = {actual!r} (expected one of {expected})")
            warnings += 1

    # Composite index checks
    missing_idx = [n for n, _, _ in COMPOSITE_INDEXES if n not in indexes]
    present_idx = [n for n, _, _ in COMPOSITE_INDEXES if n in indexes]
    if not missing_idx:
        _ok(f"Composite indexes present: {len(present_idx)}/{len(COMPOSITE_INDEXES)}")
    else:
        for n in missing_idx:
            _warn(f"Composite index missing: {n}")
            warnings += 1
    if verbose:
        for n in present_idx:
            _info(f"  index: {n}")

    click.echo("")
    click.echo("Disk")
    click.echo("-" * 60)
    try:
        if hasattr(shutil, "disk_usage"):
            usage = shutil.disk_usage(str(db_path.parent))
            free_pct = (usage.free / usage.total) * 100
            if free_pct < 5:
                _err(f"Data volume free: {_human_bytes(usage.free)} ({free_pct:.1f}%)")
                errors += 1
            elif free_pct < 15:
                _warn(f"Data volume free: {_human_bytes(usage.free)} ({free_pct:.1f}%)")
                warnings += 1
            else:
                _ok(f"Data volume free: {_human_bytes(usage.free)} ({free_pct:.1f}%)")
    except Exception as e:
        _warn(f"Could not check disk: {e}")
        warnings += 1

    # ----- Audit log health ---------------------------------------------
    click.echo("")
    click.echo("Audit log")
    click.echo("-" * 60)
    try:
        from .audit_rotation import resolve_archive_dir, rotation_status
        from .config import settings as _settings

        archive_dir = resolve_archive_dir(
            _settings.audit_archive_dir, _settings.database_url
        )
        status = asyncio.run(rotation_status(engine, archive_dir))
        _ok(f"audit_logs rows: {status['live_rows']:,}")
        if status["live_earliest"] and status["live_latest"]:
            _info(f"range: {status['live_earliest']} to {status['live_latest']}")
        if status["archive_months_archived"]:
            _ok(
                f"Archives: {status['archive_months_archived']} files, "
                f"{_human_bytes(status['archive_total_bytes'])} "
                f"({archive_dir})"
            )
        else:
            _info(f"No archives yet (archive dir: {archive_dir})")
        if status["live_rows"] > 1_000_000:
            _warn("audit_logs > 1M rows — run `aihub rotate-audit`")
            warnings += 1
    except Exception as e:
        _warn(f"Could not query audit state: {e}")
        warnings += 1

    # ----- Summary ------------------------------------------------------
    click.echo("")
    click.echo("=" * 60)
    if errors:
        click.secho(f"FAIL: {errors} error(s), {warnings} warning(s)", fg="red", bold=True)
        sys.exit(1)
    elif warnings:
        click.secho(f"OK with {warnings} warning(s)", fg="yellow", bold=True)
        sys.exit(0)
    else:
        click.secho("All checks passed.", fg="green", bold=True)
        sys.exit(0)


# ---------------------------------------------------------------------------
# Root command
# ---------------------------------------------------------------------------
@click.group(help="aihub on-prem CLI: backup, doctor, and operator tools.")
@click.version_option("1.0.0", prog_name="aihub")
def cli() -> None:
    pass


# ---------------------------------------------------------------------------
# aihub rotate-audit
# ---------------------------------------------------------------------------
@click.command(name="rotate-audit")
@click.option("--dry-run", is_flag=True, help="Report what would happen; don't modify anything.")
@click.option(
    "--retention-months",
    type=int,
    default=None,
    help="Override audit_retention_months from settings.",
)
@click.option(
    "--archive-dir",
    type=click.Path(file_okay=False, resolve_path=True),
    default=None,
    help="Override the archive directory.",
)
def rotate_audit(dry_run: bool, retention_months: int | None, archive_dir: str | None) -> None:
    """Archive + delete audit_logs rows older than the retention window.

    Use --dry-run first to see what would happen. Archives land under
    <data_dir>/archives/audit_YYYY_MM.jsonl.gz, one file per month, gzipped.
    Existing archives are appended to (safe across reruns / crashes).
    """
    import asyncio
    from .audit_rotation import resolve_archive_dir, rotate_audit_logs
    from .config import settings
    from .database import engine

    months = retention_months if retention_months is not None else settings.audit_retention_months
    arc = Path(archive_dir) if archive_dir else resolve_archive_dir(
        settings.audit_archive_dir, settings.database_url
    )

    click.echo(f"Cutoff: {months} months  •  Archive dir: {arc}  •  Dry-run: {dry_run}")
    click.echo("")

    async def _go():
        return await rotate_audit_logs(
            engine, archive_dir=arc, retention_months=months, dry_run=dry_run
        )

    summary = asyncio.run(_go())

    if summary.error:
        _err(summary.error)
        sys.exit(1)

    if not summary.months_examined:
        _ok("Nothing to rotate — all live data is within the retention window.")
        return

    click.echo(f"Months examined: {len(summary.months_examined)}")
    for m in summary.months_examined:
        marker = "WOULD ARCHIVE" if dry_run else "archived"
        click.echo(f"  {m}  {marker}")

    click.echo("")
    if dry_run:
        _ok(f"Dry-run: would archive ~{summary.rows_archived:,} rows "
            f"across {len(summary.months_archived)} months.")
    else:
        _ok(f"Archived {summary.rows_archived:,} rows  "
            f"({_human_bytes(summary.bytes_written)} written)")
        _ok(f"Deleted {summary.rows_deleted:,} rows from audit_logs")
        for f in summary.files_written:
            _info(f"file: {f}")


# ---------------------------------------------------------------------------
# aihub license — show + issue
# ---------------------------------------------------------------------------
@click.group(name="license", help="License key management.")
def license_group() -> None:
    pass


@license_group.command("show")
def license_show() -> None:
    """Print the active license (env or data/license.key)."""
    from .licensing import license as lic
    info = lic.current_license()
    click.echo(f"Customer:     {info.sub}")
    click.echo(f"License id:   {info.license_id}")
    click.echo(f"Tier:         {info.tier}")
    click.echo(f"Status:       {info.status}")
    if info.issue:
        click.echo(f"Issue:        {info.issue}")
    click.echo(f"Seats:        {'unlimited' if info.seats == 0 else info.seats}")
    if info.is_perpetual:
        click.echo(f"Expires:      never (perpetual)")
    else:
        from datetime import datetime, timezone
        click.echo(f"Expires:      {datetime.fromtimestamp(info.expires_at, timezone.utc).date()}")
        click.echo(f"Days left:    {info.days_remaining}")
    click.echo(f"Features:     {', '.join(info.features) if info.features else '(none)'}")
    if info.fingerprint:
        click.echo(f"Fingerprint:  {info.fingerprint}")


@license_group.command("issue")
@click.option("--customer", required=True, help="Customer/org name (sub).")
@click.option("--seats", default=0, type=int, help="Max named users (0 = unlimited).")
@click.option(
    "--tier",
    type=click.Choice(["trial", "starter", "pro", "enterprise"]),
    default="trial",
)
@click.option("--days", default=30, type=int, help="Validity in days (0 = perpetual).")
@click.option(
    "--feature", "features", multiple=True,
    help="Feature flag (repeatable). Use 'all' for everything.",
)
@click.option(
    "--save-to",
    type=click.Path(),
    help="Write the token to this file. Default: print to stdout.",
)
def license_issue(customer, seats, tier, days, features, save_to) -> None:
    """Issue a license JWT (signed with the dev secret).

    For production licensing replace LICENSE_SIGNING_SECRET in licensing/license.py
    with an RS256 keypair and ship only the public verify key in the binary.
    """
    from .licensing import license as lic
    token = lic.issue_license(
        sub=customer, seats=seats, tier=tier,
        days_valid=days, features=list(features),
    )
    if save_to:
        Path(save_to).write_text(token, encoding="utf-8")
        _ok(f"License written to {save_to}")
    else:
        click.echo(token)


@license_group.command("install")
@click.argument("token_file", type=click.Path(exists=True))
def license_install(token_file) -> None:
    """Install a license file at data/license.key so the platform picks it up
    on next start. Validates before installing."""
    from .licensing import license as lic
    from .config import settings

    token = Path(token_file).read_text(encoding="utf-8").strip()
    info = lic.parse_license_token(token)
    if not info.is_active:
        _err(f"License rejected: {info.issue or info.status}")
        sys.exit(1)

    dst = Path(settings.app_data_dir) / "license.key"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(token, encoding="utf-8")
    _ok(f"Installed license for {info.sub} ({info.tier}) at {dst}")


# ---------------------------------------------------------------------------
# aihub upgrade — run DB migrations / catch-up for a new platform version
# ---------------------------------------------------------------------------
@click.command(name="upgrade")
@click.option("--backup-first/--no-backup-first", default=True,
              help="Take a backup before migrating (default: yes).")
def upgrade(backup_first: bool) -> None:
    """Bring the platform DB up to date after installing a new version.

    Runs the idempotent column + index migrations (the same ones init_db runs
    at startup), but as an explicit operator step so upgrades are auditable.
    Takes a safety backup first unless --no-backup-first.
    """
    import asyncio

    if backup_first:
        click.echo("Taking a pre-upgrade backup...")
        try:
            ts = "preupgrade"
            src_db = _platform_db_path()
            dest = src_db.parent / f"aihub-backup-{ts}.tar.gz"
            staging = Path(tempfile.mkdtemp(prefix="aihub-upgrade-"))
            try:
                snap = staging / "platform.db"
                _online_backup_sqlite(src_db, snap)
                with tarfile.open(dest, "w:gz") as tar:
                    tar.add(snap, arcname="platform.db")
                _ok(f"Backup: {dest}")
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        except Exception as e:
            _warn(f"Backup failed ({e}); continuing with upgrade anyway")

    click.echo("Running migrations...")

    async def _go():
        from .database import init_db
        await init_db()

    try:
        asyncio.run(_go())
        _ok("Platform DB is up to date.")
    except Exception as e:
        _err(f"Upgrade failed: {e}")
        sys.exit(1)


cli.add_command(backup)
cli.add_command(doctor)
cli.add_command(rotate_audit)
cli.add_command(license_group)
cli.add_command(upgrade)


def main() -> None:
    """Console-script entry point."""
    cli()


if __name__ == "__main__":
    main()
