"""Build a versioned app source tree into a deployable dist tarball."""
import asyncio
import logging
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

from ..config import settings

logger = logging.getLogger(__name__)


def _builds_dir(app_id: str) -> Path:
    return Path(settings.app_data_dir) / app_id / "builds"


def _version_source_dir(app_id: str, version: int) -> Path:
    return Path(settings.app_data_dir) / app_id / "versions" / f"v{version}"


def artifact_path(app_id: str, version: int) -> Path:
    return _builds_dir(app_id) / f"v{version}.tar.gz"


def has_artifact(app_id: str, version: int) -> bool:
    return artifact_path(app_id, version).exists()


async def build_app(app_id: str, version: int, force: bool = False) -> Path:
    """Run `npm install` + `npm run build` against the version snapshot
    and tar the resulting dist/ directory.

    Returns the path to the tarball. Cached per (app_id, version) unless force=True.
    """
    out = artifact_path(app_id, version)
    if out.exists() and not force:
        logger.info("Reusing cached build artifact %s", out)
        return out

    src = _version_source_dir(app_id, version)
    if not src.exists():
        raise FileNotFoundError(f"Version source missing: {src}")

    out.parent.mkdir(parents=True, exist_ok=True)

    def _run() -> None:
        from .. import node_env
        npm_cmd = node_env.npm_cmd()
        env = {
            **_passthrough_env(),
            "VITE_AIHUB_BASE_URL": settings.vite_aihub_base_url,
        }
        if not (src / "node_modules").exists():
            logger.info("npm install for %s v%d", app_id, version)
            r = subprocess.run(
                [npm_cmd, "install", "--no-audit", "--no-fund"],
                cwd=str(src), capture_output=True, env=env,
                timeout=settings.deployer_command_timeout,
            )
            if r.returncode != 0:
                raise RuntimeError(
                    f"npm install failed (code {r.returncode}): "
                    f"{r.stderr.decode(errors='replace')[:500]}"
                )

        logger.info("npm run build for %s v%d", app_id, version)
        r = subprocess.run(
            [npm_cmd, "run", "build"],
            cwd=str(src), capture_output=True, env=env,
            timeout=settings.deployer_command_timeout,
        )
        if r.returncode != 0:
            raise RuntimeError(
                f"npm run build failed (code {r.returncode}): "
                f"{r.stderr.decode(errors='replace')[:500]}"
            )

        dist = src / "dist"
        if not dist.exists():
            raise RuntimeError("Build succeeded but dist/ does not exist")

        # Inject identity meta tags into dist/index.html so the deployed app
        # (and its SDK) can recover its app_id / version without depending on
        # the local preview proxy's window.__AIHUB_*__ injection.
        inject_identity_meta(dist / "index.html", app_id=app_id, version=version)

        # Tar the dist/ directory at the top level of the archive.
        tmp_out = out.with_suffix(out.suffix + ".tmp")
        with tarfile.open(tmp_out, "w:gz") as tf:
            tf.add(dist, arcname="dist")
        tmp_out.replace(out)

    await asyncio.get_event_loop().run_in_executor(None, _run)
    logger.info("Built artifact %s (%.1f KB)", out, out.stat().st_size / 1024)
    return out


# Tags we own. Stripped on re-injection so cached builds get the latest values.
_INJECTED_META_RE = re.compile(
    r'\s*<meta\s+name="aihub-(?:app-id|version)"[^>]*>',
    re.IGNORECASE,
)


def inject_identity_meta(index_html: Path, *, app_id: str, version: int) -> None:
    """Add `<meta name="aihub-app-id">` and `<meta name="aihub-version">` to dist/index.html.

    Idempotent: existing aihub-* meta tags are removed before re-insertion.
    No-op if the file is missing (so single-page apps with non-standard build
    outputs don't fail the whole deploy).
    """
    if not index_html.exists():
        logger.warning("inject_identity_meta: %s missing, skipping", index_html)
        return

    html = index_html.read_text(encoding="utf-8")
    html = _INJECTED_META_RE.sub("", html)

    # HTML-escape the app_id defensively even though uuids are safe.
    safe_id = app_id.replace('"', "&quot;").replace("<", "&lt;")
    tags = (
        f'\n    <meta name="aihub-app-id" content="{safe_id}">\n'
        f'    <meta name="aihub-version" content="{int(version)}">'
    )

    if "<head>" in html:
        html = html.replace("<head>", "<head>" + tags, 1)
    elif "<head " in html:
        # head tag with attributes — preserve them
        html = re.sub(r"(<head\b[^>]*>)", r"\1" + tags, html, count=1, flags=re.IGNORECASE)
    else:
        # No <head> at all — inject before </html> or just prepend
        if "</html>" in html.lower():
            html = re.sub(r"(</html>)", tags + r"\1", html, count=1, flags=re.IGNORECASE)
        else:
            html = tags + html

    index_html.write_text(html, encoding="utf-8")


def _passthrough_env() -> dict:
    """Subset of os.environ that npm needs (PATH, NODE_PATH, etc.)."""
    import os
    keep = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE",
            "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
            "HOME", "LANG", "LC_ALL", "NODE_PATH"}
    return {k: v for k, v in os.environ.items() if k.upper() in keep}


def remove_artifact(app_id: str, version: int) -> None:
    out = artifact_path(app_id, version)
    out.unlink(missing_ok=True)


def remove_all_artifacts(app_id: str) -> None:
    builds = _builds_dir(app_id)
    if builds.exists():
        shutil.rmtree(builds, ignore_errors=True)
