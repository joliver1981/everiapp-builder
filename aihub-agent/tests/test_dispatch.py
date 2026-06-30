"""Tests for the __main__.py subcommand dispatcher.

This is what makes a single aihub-agent.exe usable for both the main agent
server AND the per-app static_serve subprocess. The static_serve subcommand
is invoked by apps.py when running PyInstaller-frozen.
"""
import sys
from unittest.mock import patch

import pytest

from aihub_agent import __main__ as main_mod


def test_unknown_argv_falls_through_to_server():
    """`aihub-agent random-unknown-arg` should NOT be treated as a subcommand."""
    with patch.object(main_mod, "_run_server", return_value=42) as srv, \
         patch.object(main_mod, "_run_static_serve") as ss, \
         patch.object(sys, "argv", ["aihub-agent.exe", "random-unknown-arg"]):
        rc = main_mod.main()
    assert rc == 42
    srv.assert_called_once()
    ss.assert_not_called()


def test_no_args_runs_server():
    with patch.object(main_mod, "_run_server", return_value=0) as srv, \
         patch.object(main_mod, "_run_static_serve") as ss, \
         patch.object(sys, "argv", ["aihub-agent.exe"]):
        main_mod.main()
    srv.assert_called_once()
    ss.assert_not_called()


def test_serve_subcommand_runs_server_and_strips_argv():
    captured: list[list[str]] = []

    def fake_server():
        # The server should see argv WITHOUT the 'serve' token.
        captured.append(list(sys.argv))
        return 0

    with patch.object(main_mod, "_run_server", side_effect=fake_server), \
         patch.object(sys, "argv", ["aihub-agent.exe", "serve", "--something"]):
        main_mod.main()
    assert captured == [["aihub-agent.exe", "--something"]]


def test_static_serve_subcommand_dispatches_and_strips_argv():
    """The 'static-serve' subcommand is what apps.py invokes when frozen.
    static_serve.main() must see clean argv (no 'static-serve' token)."""
    captured: list[list[str]] = []

    def fake_static():
        captured.append(list(sys.argv))
        return 0

    with patch("aihub_agent.static_serve.main", side_effect=fake_static), \
         patch.object(sys, "argv", ["aihub-agent.exe", "static-serve", "--dir", "x", "--port", "9101"]):
        main_mod.main()
    # static_serve.main should see argv without the subcommand token
    assert captured == [["aihub-agent.exe", "--dir", "x", "--port", "9101"]]


def test_apps_spawn_uses_subcommand_when_frozen(monkeypatch):
    """The per-app static_serve spawn must produce different argv depending on
    whether we're running from source or as a frozen exe."""
    from aihub_agent import apps as apps_mod
    # We don't actually want to spawn anything — just snapshot the cmd that
    # would be built. Easiest: monkeypatch subprocess.Popen to capture.
    captured: list[list[str]] = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            captured.append(list(cmd))
            self.stdout = None
            self.returncode = 0
        def poll(self): return 0
        def wait(self, timeout=None): return 0
        def terminate(self): pass
        def kill(self): pass
        def send_signal(self, sig): pass

    monkeypatch.setattr(apps_mod.subprocess, "Popen", FakePopen)
    # Don't actually wait for vite ready — make _wait_for_ready return False fast
    async def fast_fail(port, timeout=1):
        return False
    monkeypatch.setattr(apps_mod, "_wait_for_ready", fast_fail)

    # --- Test 1: not frozen — should use python -m ---
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(sys, "executable", "/fake/python")
    import asyncio
    asyncio.run(apps_mod.registry.deploy(
        app_id="t1", version=1, port=9101,
        serve_dir=apps_mod.Path("/tmp/fake"),
    ))
    assert any("-m" in cmd and "aihub_agent.static_serve" in cmd for cmd in captured), \
        f"dev mode should use -m: {captured}"
    captured.clear()

    # --- Test 2: frozen — should use the static-serve subcommand ---
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/fake/aihub-agent.exe")
    asyncio.run(apps_mod.registry.deploy(
        app_id="t2", version=1, port=9102,
        serve_dir=apps_mod.Path("/tmp/fake"),
    ))
    assert any("static-serve" in cmd and "-m" not in cmd for cmd in captured), \
        f"frozen mode should use the static-serve subcommand: {captured}"
