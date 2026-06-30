"""Resolve the Node.js toolchain (node / npm / npx), preferring a copy vendored
with the install.

The Windows installer vendors Node under the install's app/node/ directory (see
installer/Build_AIHub_Builder.bat) so a packaged AIHub never depends on the user
having Node installed, and never conflicts with a Node they happen to have.

Resolution order (first hit wins):
  1. $AIHUB_NODE_DIR              -- explicit override (tests, custom layouts)
  2. <frozen exe dir>/node/       -- the vendored copy in a packaged install
  3. the system Node on PATH      -- developer checkouts

``ensure_on_path()`` prepends the vendored dir to this process's PATH so that
npm/npx (which shell out to their sibling ``node.exe``) and any subprocess that
inherits ``os.environ`` resolve the bundled Node too.
"""
from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

_IS_WIN = sys.platform == "win32"


@lru_cache(maxsize=1)
def bundled_node_dir() -> Path | None:
    """Directory holding the vendored Node binaries, or None if not present."""
    override = os.environ.get("AIHUB_NODE_DIR")
    if override and Path(override).is_dir():
        return Path(override)
    # PyInstaller sets sys.frozen; the build drops Node next to the exe.
    if getattr(sys, "frozen", False):
        cand = Path(sys.executable).resolve().parent / "node"
        node_bin = cand / ("node.exe" if _IS_WIN else "bin/node")
        if node_bin.exists():
            return cand
    return None


def _resolve(win_name: str, posix_rel: str, path_fallback: str) -> str:
    d = bundled_node_dir()
    if d:
        p = d / (win_name if _IS_WIN else posix_rel)
        if p.exists():
            return str(p)
    return path_fallback


def node_cmd() -> str:
    # NODE_PATH honored as a legacy explicit override (dev machines that set it).
    fallback = os.environ.get("NODE_PATH") or ("node.exe" if _IS_WIN else "node")
    return _resolve("node.exe", "bin/node", fallback)


def npm_cmd() -> str:
    return _resolve("npm.cmd", "bin/npm", "npm.cmd" if _IS_WIN else "npm")


def npx_cmd() -> str:
    return _resolve("npx.cmd", "bin/npx", "npx.cmd" if _IS_WIN else "npx")


def ensure_on_path() -> None:
    """Prepend the vendored Node dir to this process's PATH (idempotent).

    No-op when there is no vendored Node (developer checkouts), so the system
    Node on PATH is used unchanged.
    """
    d = bundled_node_dir()
    if not d:
        return
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if str(d) not in parts:
        os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
