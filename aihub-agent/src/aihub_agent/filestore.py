import shutil
import tarfile
from pathlib import Path
from .config import settings


def app_version_dir(app_id: str, version: int) -> Path:
    return settings.apps_dir / app_id / f"v{version}"


def write_artifact(app_id: str, version: int, tarball_bytes: bytes) -> Path:
    """Unpack a dist tarball into the per-version directory.

    Returns the path that should be served (the unpacked dist/).
    Removes any prior contents at that version path so the deploy is clean.
    """
    target = app_version_dir(app_id, version)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    tmp_tar = target.parent / f"v{version}.tar.gz"
    tmp_tar.write_bytes(tarball_bytes)

    try:
        with tarfile.open(tmp_tar, "r:gz") as tf:
            _safe_extract(tf, target)
    finally:
        tmp_tar.unlink(missing_ok=True)

    # Tarballs from the AIHub builder root the dist/ dir at the top level.
    inner = target / "dist"
    return inner if inner.exists() else target


def remove_app_dir(app_id: str) -> None:
    app_dir = settings.apps_dir / app_id
    if app_dir.exists():
        shutil.rmtree(app_dir, ignore_errors=True)


def _safe_extract(tf: tarfile.TarFile, target: Path) -> None:
    target_resolved = target.resolve()
    for member in tf.getmembers():
        member_path = (target / member.name).resolve()
        if not str(member_path).startswith(str(target_resolved)):
            raise ValueError(f"Tarball entry escapes target dir: {member.name}")
    tf.extractall(target)
