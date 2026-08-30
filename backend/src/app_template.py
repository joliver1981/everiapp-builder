"""Locate the app-template scaffold (the tree copied into every new app) and
repair app trees that were scaffolded while it couldn't be found.

Resolution order (first hit wins):
  1. $AIHUB_APP_TEMPLATE_DIR      -- explicit override (tests, custom layouts)
  2. sys._MEIPASS/app-template    -- packaged installs: installer/aihub.spec
                                     bundles the template into the PyInstaller
                                     onedir data tree (the _internal/ dir)
  3. <repo root>/app-template     -- developer checkouts

The frozen branch exists because __file__-relative resolution does NOT hold in
a PyInstaller build: src/* modules resolve under sys._MEIPASS (…/_internal),
so the old "four parents up" walk landed next to the exe, where no
app-template exists. The scaffold's silent minimal fallback then created husk
apps (src/App.tsx only — no package.json, no vite config) that failed their
first verify with a baffling `npm error enoent … draft\\frontend\\package.json`.
Same mechanism as node_env/python_env for the vendored toolchains and
functions/service._harness_path for the runner.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Derived/reproducible state never copied from the template into an app tree
# (installer/aihub.spec skips the same directories when bundling the template).
SCAFFOLD_IGNORE = ("node_modules", "dist", ".git", "__pycache__", "tsconfig.tsbuildinfo")


def template_root() -> Path:
    """Directory holding the app scaffold. Existence is NOT guaranteed —
    callers that copy from it must check and fail loudly, never silently."""
    override = os.environ.get("AIHUB_APP_TEMPLATE_DIR")
    if override:
        return Path(override)
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            return Path(meipass) / "app-template"
    return Path(__file__).resolve().parents[2] / "app-template"


def heal_missing_scaffold(app_dir: Path, template_dir: Path | None = None) -> list[str]:
    """Copy template files that are MISSING from an app tree. Never overwrites.

    Repairs drafts scaffolded while the template couldn't be located (the
    frozen-path bug above): the next verify or preview start restores
    package.json / vite.config.ts / index.html / the vendored SDK while
    leaving every AI-written file untouched. Safe on any draft: generation has
    no delete action and there is no file-delete endpoint, so a missing
    scaffold file always means a broken scaffold, not intent — and it doubles
    as "old apps gain files the template added since". Returns the relative
    paths it added. Draft trees only — version snapshots are immutable.
    """
    t = template_root() if template_dir is None else template_dir
    if not t.is_dir() or not app_dir.is_dir():
        return []
    skip = set(SCAFFOLD_IGNORE)
    added: list[str] = []
    for root, dirnames, filenames in os.walk(t):
        dirnames[:] = [d for d in dirnames if d not in skip]
        rel_root = Path(root).relative_to(t)
        for fname in filenames:
            if fname in skip:
                continue
            dst = app_dir / rel_root / fname
            if dst.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(root) / fname, dst)
            added.append((rel_root / fname).as_posix())
    if added:
        logger.warning("healed %d missing scaffold file(s) in %s: %s",
                       len(added), app_dir, ", ".join(added[:12]))
    return added
