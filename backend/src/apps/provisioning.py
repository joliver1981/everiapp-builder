"""Fast node_modules provisioning for app trees.

POST /api/apps used to copy the template's node_modules (~140MB / 11.5k files)
into every new app's draft at scaffold time so the first preview/verify could
skip npm install — which put ~40s on the app-creation critical path. Scaffold
no longer copies node_modules; instead, the first consumer that actually needs
dependencies (runtime preview start, AI verifier) calls
try_copy_template_node_modules(): an offline copy of the template's
node_modules, taken only when the app's declared dependencies still match the
template's. Anything else (an app whose package.json drifted, a checkout whose
template was never npm-installed) falls back to the caller's real npm install.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "app-template"


def _declared_deps(package_json: Path) -> tuple[dict, dict] | None:
    """(dependencies, devDependencies) from a package.json, or None if unreadable."""
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return (data.get("dependencies") or {}, data.get("devDependencies") or {})


def template_satisfies(app_dir: Path, template_dir: Path | None = None) -> bool:
    """True when the template's node_modules can stand in for app_dir's.

    Requires the template to actually have node_modules AND both package.json
    files to declare identical dependencies/devDependencies — an app whose deps
    drifted needs its own npm install (matching today's behavior for such apps).
    """
    t = TEMPLATE_DIR if template_dir is None else template_dir
    if not (t / "node_modules").is_dir():
        return False
    t_deps = _declared_deps(t / "package.json")
    a_deps = _declared_deps(app_dir / "package.json")
    return t_deps is not None and t_deps == a_deps


async def try_copy_template_node_modules(app_dir: Path, template_dir: Path | None = None) -> bool:
    """Provision app_dir/node_modules from the template's copy if possible.

    Returns True when app_dir/node_modules is ready afterwards (copied now, or
    already present). False means the caller must npm install. The copy lands
    in a temp sibling and is renamed into place, so an interrupted copy never
    leaves a half-populated node_modules that a later start would mistake for
    complete; a concurrent provisioner winning the rename race counts as done.
    """
    dest = app_dir / "node_modules"
    if dest.exists():
        return True
    t = TEMPLATE_DIR if template_dir is None else template_dir
    if not template_satisfies(app_dir, t):
        return False

    tmp = app_dir / f".node_modules.partial-{os.getpid()}"

    def _copy() -> bool:
        if tmp.exists():
            shutil.rmtree(tmp)
        try:
            shutil.copytree(t / "node_modules", tmp)
            tmp.rename(dest)
            return True
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)
            return dest.exists()  # lost a race to another provisioner → still ready

    try:
        ok = await asyncio.to_thread(_copy)
    except Exception:
        logger.exception("template node_modules copy failed for %s", app_dir)
        return False
    if ok:
        logger.info("Provisioned node_modules for %s from app-template", app_dir)
    return ok
