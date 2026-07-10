"""Admin-managed Python packages for the server-function environment.

The managed directory (python_env.managed_packages_dir()) is an extra
site-packages the function child interpreter gets on sys.path (after the
app's own server/ files, before the interpreter's paths — so an admin install
can deliberately shadow a bundled curated package). Installs are ADDITIVE
`pip install --target` runs into the live dir (safe for in-flight children:
nothing is removed, and a package is only announced after pip exits 0);
uninstall rebuilds the whole dir from the manifest of remaining rows into a
fresh sibling and swaps it in (pip cannot uninstall from a --target dir).

One operation runs at a time (module asyncio.Lock; concurrent request → 409).
Because every status transition happens while the lock is held IN THIS
process, "row in a transient status while the lock is free" is a complete
definition of "orphaned by a restart" — reconciliation is lazy and trivial.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import python_env
from ..config import settings
from ..platform_settings.service import get_setting
from ..secrets.models import AuditLog
from .models import PythonPackage

logger = logging.getLogger(__name__)

# The curated set the installer lays down (packaged: <exe>/python-libs; dev:
# the platform venv via pyproject [server-fns]). MIRROR CONTRACT: this tuple,
# backend/pyproject.toml [server-fns], the ai/prompts.py Server Functions
# section, and installer/Build_AIHub_Builder.bat step [2c] name the same set —
# change them together.
BUNDLED_PACKAGES = ("pandas", "numpy", "openpyxl", "reportlab", "pypdf", "python-dateutil")

NAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$")
# PEP 440 charset incl. epochs (1!2.0), post/dev tags, local versions (+cpu).
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._!+-]{0,98}$")

TRANSIENT_STATUSES = ("pending", "installing", "uninstalling")

# One package operation at a time, process-wide. Acquired in the start_*
# entrypoints, released by the background task's finally (asyncio.Lock allows
# cross-task release).
_job_lock = asyncio.Lock()


class PackageError(Exception):
    """Client-correctable problem. Maps to 4xx/5xx with a fixable message."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _validate(name: str, version: str | None) -> tuple[str, str]:
    """Returns (normalized_name, pinned_version). The spec handed to pip is
    RECONSTRUCTED from these — the raw user string never reaches argv, so no
    URLs, paths, options, or whitespace can ride along."""
    if not name or not NAME_RE.match(name):
        raise PackageError(
            "Invalid package name — use the exact PyPI project name "
            "(letters, digits, '.', '_', '-'; no spaces, URLs, or paths).")
    pin = (version or "").strip()
    if pin and not VERSION_RE.match(pin):
        raise PackageError(
            "Invalid version — pass a plain version like 1.2.3 "
            "(no ranges; '>=', '~=' etc. are not supported).")
    return _normalize(name), pin


def _run_pip(args: list[str]) -> tuple[int, str]:
    """The ONLY place pip actually runs (sync; callers wrap in to_thread).
    Kept as a module attribute so tests can fake the seam. pip is trusted
    platform tooling running no third-party code under --only-binary, and it
    needs the full environment (corporate proxies, CA bundles) — unlike
    function children, no env whitelist."""
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    try:
        cp = subprocess.run(
            args, capture_output=True, text=True,
            timeout=settings.pip_command_timeout, creationflags=creationflags,
        )
        out = (cp.stdout or "") + ("\n" + cp.stderr if cp.stderr else "")
        return cp.returncode, out
    except subprocess.TimeoutExpired:
        return 124, (f"pip timed out after {settings.pip_command_timeout}s — very large "
                     "packages may need PIP_COMMAND_TIMEOUT raised.")


def _pip_install_args(target: Path, specs: list[str], index_url: str) -> list[str]:
    cmd = python_env.pip_cmd()
    assert cmd, "callers check pip availability first"
    args = cmd + [
        "install",
        "--target", str(target),
        # Required for re-install/upgrade into an existing --target dir; also
        # gives deliberate version-change semantics.
        "--upgrade",
        # Wheels only: no compiler needed AND no sdist code execution at
        # install time.
        "--only-binary=:all:",
        "--no-input", "--disable-pip-version-check",
        "--no-warn-script-location",
        # The service account may lack a usable pip cache dir.
        "--no-cache-dir",
    ]
    if index_url:
        args += ["--index-url", index_url]
    return args + specs


def _scan_dist_info(directory: Path) -> dict[str, str]:
    """normalized name → version, from *.dist-info directory names."""
    out: dict[str, str] = {}
    if not directory.is_dir():
        return out
    for d in directory.iterdir():
        if not d.is_dir() or not d.name.endswith(".dist-info"):
            continue
        stem = d.name[: -len(".dist-info")]
        name, sep, version = stem.rpartition("-")
        if sep and name:
            out[_normalize(name)] = version
    return out


def _sweep_trash() -> None:
    """Best-effort removal of swap leftovers from prior jobs. They are never
    on any sys.path, so a locked one waiting another cycle is harmless."""
    root = python_env.managed_packages_dir().parent
    if not root.is_dir():
        return
    for d in root.glob("server-packages.trash-*"):
        shutil.rmtree(d, ignore_errors=True)
    for d in root.glob("server-packages.new-*"):
        shutil.rmtree(d, ignore_errors=True)


def _bundled_versions() -> dict[str, str]:
    """Real installed versions of the curated set. The frozen platform's own
    importlib.metadata reflects the PyInstaller bundle, NOT the child env —
    there we scan the vendored python-libs dist-info instead."""
    if getattr(sys, "frozen", False):
        libs = Path(sys.executable).resolve().parent / "python-libs"
        scanned = _scan_dist_info(libs)
        return {name: scanned.get(_normalize(name), "") for name in BUNDLED_PACKAGES}
    import importlib.metadata
    out = {}
    for name in BUNDLED_PACKAGES:
        try:
            out[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            out[name] = ""
    return out


async def _reconcile_stuck_rows(db: AsyncSession) -> None:
    """Rows left in a transient status by a restart (lock free ⇒ no job is
    actually running in this process) become failed with a fixable message."""
    if _job_lock.locked():
        return
    rows = (await db.execute(select(PythonPackage).where(
        PythonPackage.status.in_(TRANSIENT_STATUSES)))).scalars().all()
    if not rows:
        return
    for row in rows:
        row.status = "failed"
        row.error = "Interrupted by a platform restart — install again or press Rebuild."
    await db.commit()


def _row_dict(row: PythonPackage) -> dict:
    return {
        "name": row.name,
        "version": row.installed_version,
        "source": "admin",
        "status": row.status,
        "error": row.error,
        "requested_spec": row.requested_spec,
        "pinned_version": row.pinned_version,
        "requested_by": row.requested_by,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def list_inventory(db: AsyncSession) -> dict:
    await _reconcile_stuck_rows(db)
    rows = (await db.execute(
        select(PythonPackage).order_by(PythonPackage.name))).scalars().all()

    managed = python_env.managed_packages_dir()
    scanned = _scan_dist_info(managed)

    packages = [
        {"name": name, "version": version, "source": "bundled", "status": "installed",
         "error": "", "requested_spec": None, "pinned_version": None,
         "requested_by": None, "updated_at": None}
        for name, version in sorted(_bundled_versions().items())
    ]
    for row in rows:
        d = _row_dict(row)
        # dist-info is truth for the version on disk; the row is truth for
        # status/error (e.g. a failed pin attempt on an installed package).
        d["version"] = scanned.get(row.name, row.installed_version)
        packages.append(d)

    pip_available = python_env.pip_cmd() is not None
    index_url = str(await get_setting(db, "pip_index_url") or "")
    return {
        "packages": packages,
        "environment": {
            "python_version": python_env.child_python_version(),
            "python_path": python_env.python_cmd() or "",
            "pip_available": pip_available,
            "managed_dir": str(managed),
            "index_url": index_url,
            # Transitive dependencies pulled in by admin installs — no rows,
            # cleaned up by the uninstall/rebuild path.
            "dependency_count": max(0, len(scanned) - sum(1 for r in rows if r.name in scanned)),
            "busy": _job_lock.locked(),
        },
    }


def _require_pip() -> None:
    if python_env.pip_cmd() is None:
        raise PackageError(
            "pip is not available in this install — re-run the platform "
            "installer (it vendors pip.pyz) or set AIHUB_PYTHON_DIR to a full "
            "Python.", status_code=503)


def _require_idle() -> None:
    if _job_lock.locked():
        raise PackageError(
            "A package operation is already running — wait for it to finish.",
            status_code=409)


async def start_install(db: AsyncSession, name: str, version: str | None, user) -> dict:
    normalized, pin = _validate(name, version)
    _require_pip()
    _require_idle()
    await _reconcile_stuck_rows(db)

    spec = f"{name}=={pin}" if pin else name
    row = (await db.execute(select(PythonPackage).where(
        PythonPackage.name == normalized))).scalar_one_or_none()
    if row is None:
        row = PythonPackage(name=normalized)
        db.add(row)
    # Same normalized name = re-install/upgrade of the existing row.
    row.requested_spec = spec
    row.pinned_version = pin
    row.status = "pending"
    row.error = ""
    row.requested_by = user.id
    db.add(AuditLog(user_id=user.id, action="python_package.install",
                    resource_type="python_package", resource_id=normalized,
                    details=f"Install requested: {spec}"))
    await db.commit()
    row_id = row.id

    await _job_lock.acquire()
    asyncio.create_task(_run_install(row_id))
    return _row_dict(row)


async def _run_install(row_id: str) -> None:
    """Background worker — owns its session (route's is request-scoped), owns
    the already-acquired lock, releases it in finally."""
    from ..database import async_session
    try:
        _sweep_trash()
        managed = python_env.managed_packages_dir()
        managed.mkdir(parents=True, exist_ok=True)

        async with async_session() as db:
            row = (await db.execute(select(PythonPackage).where(
                PythonPackage.id == row_id))).scalar_one()
            row.status = "installing"
            await db.commit()
            spec = row.requested_spec
            index_url = str(await get_setting(db, "pip_index_url") or "")

        rc, out = await asyncio.to_thread(
            _run_pip, _pip_install_args(managed, [spec], index_url))

        async with async_session() as db:
            row = (await db.execute(select(PythonPackage).where(
                PythonPackage.id == row_id))).scalar_one()
            if rc == 0:
                row.status = "installed"
                row.error = ""
                row.installed_version = _scan_dist_info(managed).get(row.name, "")
                logger.info("python package installed: %s (%s)", row.requested_spec,
                            row.installed_version)
            else:
                row.status = "failed"
                # pip's own messages ("No matching distribution found for X")
                # are the most fixable text we could surface.
                row.error = out.strip()[-1500:]
                logger.warning("python package install failed: %s\n%s",
                               row.requested_spec, row.error[-400:])
            await db.commit()
    except Exception as e:
        logger.exception("python package install crashed")
        try:
            async with async_session() as db:
                row = (await db.execute(select(PythonPackage).where(
                    PythonPackage.id == row_id))).scalar_one_or_none()
                if row is not None:
                    row.status = "failed"
                    row.error = f"{type(e).__name__}: {str(e)[:500]}"
                    await db.commit()
        except Exception:
            pass
    finally:
        _job_lock.release()


async def start_uninstall(db: AsyncSession, name: str, user) -> dict:
    normalized = _normalize(name)
    if not NAME_RE.match(name or ""):
        raise PackageError("Invalid package name.")
    row = (await db.execute(select(PythonPackage).where(
        PythonPackage.name == normalized))).scalar_one_or_none()
    if row is None:
        if normalized in {_normalize(b) for b in BUNDLED_PACKAGES}:
            raise PackageError(
                f"'{normalized}' is bundled with the platform — managed by the "
                "installer, not removable here.", status_code=400)
        raise PackageError(f"No admin-installed package named '{normalized}'.",
                           status_code=404)
    _require_pip()
    _require_idle()
    await _reconcile_stuck_rows(db)

    row.status = "uninstalling"
    row.error = ""
    db.add(AuditLog(user_id=user.id, action="python_package.uninstall",
                    resource_type="python_package", resource_id=normalized,
                    details=f"Uninstall requested: {row.requested_spec}"))
    await db.commit()
    row_id = row.id

    await _job_lock.acquire()
    asyncio.create_task(_run_rebuild(exclude_row_id=row_id))
    return _row_dict(row)


async def start_rebuild(db: AsyncSession, user) -> None:
    """Manual escape hatch: one pip run over the whole manifest into a fresh
    dir (real dependency resolution) — recovers from additive-install drift,
    orphaned dependencies, and partial extracts."""
    _require_pip()
    _require_idle()
    await _reconcile_stuck_rows(db)
    db.add(AuditLog(user_id=user.id, action="python_package.rebuild",
                    resource_type="python_package", resource_id="environment",
                    details="Environment rebuild requested"))
    await db.commit()
    await _job_lock.acquire()
    asyncio.create_task(_run_rebuild())


def _swap_managed_dir(new_dir: Path) -> None:
    """Windows-safe replace of the live managed dir. Renaming a dir whose
    FILES are open usually succeeds on NTFS (handles hold the files, not the
    dir name); when it doesn't, retry briefly then raise — the job fails with
    a retry message rather than half-swapping."""
    live = python_env.managed_packages_dir()
    trash = live.with_name(f"server-packages.trash-{uuid.uuid4().hex[:8]}")
    if live.exists():
        for attempt in range(5):
            try:
                live.rename(trash)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(1)
    new_dir.rename(live)
    shutil.rmtree(trash, ignore_errors=True)


async def _run_rebuild(exclude_row_id: str | None = None) -> None:
    from ..database import async_session
    try:
        _sweep_trash()
        managed = python_env.managed_packages_dir()

        async with async_session() as db:
            rows = (await db.execute(
                select(PythonPackage).order_by(PythonPackage.name))).scalars().all()
            # Rebuild re-installs what the manifest SAYS should exist — pinned
            # rows keep their pin, unpinned re-resolve to latest. `failed` rows
            # are deliberately left out: their spec is known-bad (e.g. a pin
            # that never resolved) and would poison every rebuild; the row
            # stays visible with its error until re-installed or removed.
            keep = [r for r in rows if r.id != exclude_row_id
                    and r.status in ("installed", "pending", "installing")]
            specs = [f"{r.name}=={r.pinned_version}" if r.pinned_version else r.name
                     for r in keep]
            index_url = str(await get_setting(db, "pip_index_url") or "")

        new_dir = managed.with_name(f"server-packages.new-{uuid.uuid4().hex[:8]}")
        new_dir.mkdir(parents=True, exist_ok=True)

        rc, out = (0, "")
        if specs:
            rc, out = await asyncio.to_thread(
                _run_pip, _pip_install_args(new_dir, specs, index_url))

        async with async_session() as db:
            rows = (await db.execute(
                select(PythonPackage).order_by(PythonPackage.name))).scalars().all()
            excluded = next((r for r in rows if r.id == exclude_row_id), None)
            if rc != 0:
                shutil.rmtree(new_dir, ignore_errors=True)
                tail = out.strip()[-1500:]
                for r in rows:
                    if r.status in TRANSIENT_STATUSES:
                        r.status = "failed"
                        r.error = f"Environment rebuild failed: {tail[-500:]}"
                await db.commit()
                logger.warning("python packages rebuild failed:\n%s", tail[-400:])
                return
            try:
                _swap_managed_dir(new_dir)
            except PermissionError:
                shutil.rmtree(new_dir, ignore_errors=True)
                for r in rows:
                    if r.status in TRANSIENT_STATUSES:
                        r.status = "failed"
                        r.error = ("Could not replace the packages directory — server "
                                   "functions were executing. Retry in a moment.")
                await db.commit()
                return
            scanned = _scan_dist_info(python_env.managed_packages_dir())
            if excluded is not None:
                await db.delete(excluded)
            for r in rows:
                if r.id == exclude_row_id:
                    continue
                r.installed_version = scanned.get(r.name, r.installed_version)
                if r.status in TRANSIENT_STATUSES:
                    r.status = "installed"
                    r.error = ""
            await db.commit()
            logger.info("python packages environment rebuilt (%d specs)", len(specs))
    except Exception as e:
        logger.exception("python packages rebuild crashed")
        try:
            async with async_session() as db:
                rows = (await db.execute(select(PythonPackage).where(
                    PythonPackage.status.in_(TRANSIENT_STATUSES)))).scalars().all()
                for r in rows:
                    r.status = "failed"
                    r.error = f"{type(e).__name__}: {str(e)[:500]}"
                await db.commit()
        except Exception:
            pass
    finally:
        _job_lock.release()


# --- version lookup (best-effort; install NEVER depends on it) --------------

def _make_client() -> httpx.AsyncClient:
    # Factory so tests inject MockTransport (marketplace/external.py pattern).
    # trust_env default honors corporate HTTP(S)_PROXY / CA bundles.
    return httpx.AsyncClient(timeout=10.0, follow_redirects=True)


def _version_key(v: str):
    """Dependency-free desc-sortable key: split into int/str runs."""
    parts = re.split(r"(\d+)", v)
    return tuple((1, int(p)) if p.isdigit() else (0, p) for p in parts if p)


def _versions_from_filenames(normalized: str, filenames: list[str]) -> list[str]:
    versions: set[str] = set()
    underscored = normalized.replace("-", "_")
    for fname in filenames:
        low = fname.lower()
        if low.endswith(".whl"):
            parts = fname.split("-")
            if len(parts) >= 2:
                versions.add(parts[1])
        else:
            for ext in (".tar.gz", ".zip", ".tar.bz2"):
                if low.endswith(ext):
                    stem = fname[: -len(ext)]
                    prefix = f"{underscored}-"
                    if stem.lower().startswith(prefix):
                        versions.add(stem[len(prefix):])
                    break
    return sorted(versions, key=_version_key, reverse=True)


async def lookup(db: AsyncSession, name: str) -> dict:
    if not name or not NAME_RE.match(name):
        raise PackageError("Invalid package name.")
    normalized = _normalize(name)
    index_url = str(await get_setting(db, "pip_index_url") or "").rstrip("/")

    try:
        async with _make_client() as client:
            if not index_url:
                resp = await client.get(f"https://pypi.org/pypi/{normalized}/json")
                if resp.status_code == 404:
                    return {"available": False, "error": f"'{normalized}' not found on PyPI."}
                resp.raise_for_status()
                data = resp.json()
                releases = data.get("releases") or {}
                versions = sorted(
                    (v for v, files in releases.items()
                     if files and not all(f.get("yanked") for f in files)),
                    key=_version_key, reverse=True)
                return {
                    "available": True,
                    "name": data.get("info", {}).get("name") or normalized,
                    "normalized_name": normalized,
                    "summary": (data.get("info", {}).get("summary") or "")[:300],
                    "latest": versions[0] if versions else "",
                    "versions": versions[:50],
                }
            # Custom index: PEP 691 JSON simple API.
            resp = await client.get(
                f"{index_url}/{normalized}/",
                headers={"Accept": "application/vnd.pypi.simple.v1+json"})
            if resp.status_code == 404:
                return {"available": False,
                        "error": f"'{normalized}' not found on the configured index."}
            resp.raise_for_status()
            if "json" not in (resp.headers.get("content-type") or ""):
                return {"available": False,
                        "error": "The configured index did not return PEP 691 JSON — "
                                 "type an exact version to install anyway."}
            data = resp.json()
            filenames = [f.get("filename", "") for f in data.get("files", [])]
            versions = _versions_from_filenames(normalized, filenames)
            return {
                "available": True,
                "name": data.get("name") or normalized,
                "normalized_name": normalized,
                "summary": "",
                "latest": versions[0] if versions else "",
                "versions": versions[:50],
            }
    except httpx.HTTPError as e:
        return {"available": False, "error": f"Package lookup failed: {str(e)[:200]}"}
    except (json.JSONDecodeError, ValueError) as e:
        return {"available": False, "error": f"Package lookup failed: {str(e)[:200]}"}
