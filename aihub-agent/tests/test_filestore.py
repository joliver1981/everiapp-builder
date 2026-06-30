import io
import tarfile
from pathlib import Path

import pytest

from aihub_agent import filestore


def _make_tarball(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_unpack_serves_dist_subdir(tmp_path, monkeypatch):
    monkeypatch.setattr(filestore.settings, "agent_data_dir", str(tmp_path))
    tar = _make_tarball({
        "dist/index.html": b"<html>ok</html>",
        "dist/assets/app.js": b"console.log(1)",
    })

    serve_dir = filestore.write_artifact("app-1", 3, tar)
    assert serve_dir == filestore.app_version_dir("app-1", 3) / "dist"
    assert (serve_dir / "index.html").read_bytes() == b"<html>ok</html>"
    assert (serve_dir / "assets" / "app.js").exists()


def test_overwrite_clears_prior_contents(tmp_path, monkeypatch):
    monkeypatch.setattr(filestore.settings, "agent_data_dir", str(tmp_path))
    filestore.write_artifact("app-1", 1, _make_tarball({"dist/old.html": b"old"}))
    filestore.write_artifact("app-1", 1, _make_tarball({"dist/new.html": b"new"}))
    serve_dir = filestore.app_version_dir("app-1", 1) / "dist"
    assert (serve_dir / "new.html").exists()
    assert not (serve_dir / "old.html").exists()


def test_path_traversal_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(filestore.settings, "agent_data_dir", str(tmp_path))
    tar = _make_tarball({"../escape.txt": b"nope"})
    with pytest.raises(ValueError):
        filestore.write_artifact("app-1", 1, tar)
