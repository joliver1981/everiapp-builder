"""node_env resolves the Node toolchain, preferring a vendored copy."""
from __future__ import annotations

import os
import sys

import pytest

from src import node_env

_IS_WIN = sys.platform == "win32"
_NODE = "node.exe" if _IS_WIN else "node"
_NPM = "npm.cmd" if _IS_WIN else "npm"
_NPX = "npx.cmd" if _IS_WIN else "npx"


def _make_fake_node(dirpath) -> None:
    """Create the binaries node_env probes for, matching the platform layout."""
    if _IS_WIN:
        for name in (_NODE, _NPM, _NPX):
            (dirpath / name).write_text("echo fake")
    else:
        bind = dirpath / "bin"
        bind.mkdir()
        for name in ("node", "npm", "npx"):
            (bind / name).write_text("#!/bin/sh\n")


@pytest.fixture(autouse=True)
def _clear_cache():
    node_env.bundled_node_dir.cache_clear()
    yield
    node_env.bundled_node_dir.cache_clear()


def test_falls_back_to_path_without_bundle(monkeypatch):
    monkeypatch.delenv("AIHUB_NODE_DIR", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert node_env.bundled_node_dir() is None
    # Bare command names → resolved off PATH by subprocess.
    assert node_env.npm_cmd() == _NPM
    assert node_env.npx_cmd() == _NPX
    assert os.sep not in node_env.npm_cmd()


def test_prefers_bundled_node(tmp_path, monkeypatch):
    _make_fake_node(tmp_path)
    monkeypatch.setenv("AIHUB_NODE_DIR", str(tmp_path))
    node_env.bundled_node_dir.cache_clear()

    assert node_env.bundled_node_dir() == tmp_path
    for resolved in (node_env.node_cmd(), node_env.npm_cmd(), node_env.npx_cmd()):
        assert str(tmp_path) in resolved  # absolute path into the bundle
        assert os.path.exists(resolved)


def test_ensure_on_path_prepends_and_is_idempotent(tmp_path, monkeypatch):
    _make_fake_node(tmp_path)
    monkeypatch.setenv("AIHUB_NODE_DIR", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin")
    node_env.bundled_node_dir.cache_clear()

    node_env.ensure_on_path()
    assert os.environ["PATH"].split(os.pathsep)[0] == str(tmp_path)
    # Second call must not add a duplicate entry.
    node_env.ensure_on_path()
    assert os.environ["PATH"].split(os.pathsep).count(str(tmp_path)) == 1


def test_ensure_on_path_noop_without_bundle(monkeypatch):
    monkeypatch.delenv("AIHUB_NODE_DIR", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setenv("PATH", "/usr/bin")
    node_env.bundled_node_dir.cache_clear()
    node_env.ensure_on_path()
    assert os.environ["PATH"] == "/usr/bin"
