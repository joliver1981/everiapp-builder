"""Resolve the Python interpreter used to run app server functions.

Server functions execute in a CHILD Python process (never in the platform
process — a runaway function must be killable, and platform secrets must not
share its address space). Which interpreter runs them depends on how the
platform itself is running:

  - Packaged installs are a PyInstaller-frozen exe: sys.executable is the
    platform binary, not a Python interpreter. The Windows installer vendors a
    CPython embeddable distribution under the install's app/python/ directory
    (mirroring the vendored Node in app/node/), with its ._pth extended to
    include the curated server-function libraries in app/python-libs/.
  - Developer checkouts run under a real interpreter (the repo venv), so
    sys.executable is used directly — the curated libraries come from the
    venv via the backend's [server-fns] extra.

Resolution order (first hit wins):
  1. $AIHUB_PYTHON_DIR             -- explicit override (tests, custom layouts)
  2. <frozen exe dir>/python/      -- the vendored copy in a packaged install
  3. sys.executable                -- developer checkouts (not frozen)
"""
from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

_IS_WIN = sys.platform == "win32"


@lru_cache(maxsize=1)
def bundled_python_dir() -> Path | None:
    """Directory holding the vendored CPython, or None if not present."""
    override = os.environ.get("AIHUB_PYTHON_DIR")
    if override and Path(override).is_dir():
        return Path(override)
    if getattr(sys, "frozen", False):
        cand = Path(sys.executable).resolve().parent / "python"
        py_bin = cand / ("python.exe" if _IS_WIN else "bin/python3")
        if py_bin.exists():
            return cand
    return None


def python_cmd() -> str | None:
    """Path to the interpreter for server-function children, or None when a
    frozen install has no vendored Python (mis-packaged — callers surface a
    fixable 503, never a crash)."""
    d = bundled_python_dir()
    if d:
        p = d / ("python.exe" if _IS_WIN else "bin/python3")
        if p.exists():
            return str(p)
    if getattr(sys, "frozen", False):
        return None
    return sys.executable


def managed_packages_dir() -> Path:
    """Where admin-installed server-function packages live: a sibling of the
    apps dir inside the instance data root (dev: data/server-packages;
    packaged: <commonappdata>\\EveriApp\\data\\server-packages), so it survives
    platform upgrades and travels with the DB whose rows describe it.

    Deliberately NOT cached: tests reassign settings.app_data_dir at runtime.
    Resolved absolute BEFORE .parent — the dev value is relative ("./data/apps")
    and the child pip/function processes must never re-resolve it against
    their own cwd."""
    from .config import settings
    return Path(settings.app_data_dir).resolve().parent / "server-packages"


# The runpy shim that runs pip out of a zipapp under the EMBEDDABLE interpreter.
# Plain `python.exe pip.pyz` can't be trusted there: the embeddable's ._pth
# implies isolated path initialization, so the zipapp may never reach sys.path.
# We also deliberately do NOT add pip to the ._pth — that would make
# `import pip` available to AI-generated server-function code.
_PIP_PYZ_SHIM = (
    "import sys, runpy; "
    "sys.path.insert(0, sys.argv[1]); "
    "del sys.argv[1]; "
    "sys.argv[0] = 'pip'; "
    "runpy.run_module('pip', run_name='__main__')"
)


def pip_cmd() -> list[str] | None:
    """argv prefix that runs pip for the server-function environment, or None
    when unavailable (frozen install without the vendored pip.pyz — callers
    surface a fixable 503 pointing at re-running the installer)."""
    if not getattr(sys, "frozen", False):
        return [sys.executable, "-m", "pip"]
    d = bundled_python_dir()
    py = python_cmd()
    if d and py:
        pyz = d / "pip.pyz"
        if pyz.exists():
            return [py, "-c", _PIP_PYZ_SHIM, str(pyz)]
    return None


@lru_cache(maxsize=1)
def child_python_version() -> str:
    """The child interpreter's version string ("3.12.10"), or "" when it can't
    be determined. Cached — the interpreter can't change within a process."""
    import subprocess
    py = python_cmd()
    if not py:
        return ""
    try:
        cp = subprocess.run([py, "-V"], capture_output=True, text=True, timeout=15)
        return (cp.stdout or cp.stderr or "").replace("Python", "").strip()
    except Exception:
        return ""
