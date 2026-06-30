"""Regression guard for the preview "Process exited unexpectedly (code 1)" bug.

The runtime manager used to allocate a preview port from internal tracking only.
But the dev backend restarts a lot, which ORPHANS the Vite servers it spawned
(they keep holding their ports). Reusing such a port made the new Vite die with
--strictPort "port in use" (exit 1) while the health poll hit the orphan, so the
preview reported "Process exited unexpectedly (code 1)".

`_allocate_port` now skips ports that are ACTUALLY listening. These tests pin
that so the regression can't return.
"""
from __future__ import annotations

import socket

from src.runtime.manager import RuntimeManager


def _occupy_a_pool_port(m: RuntimeManager) -> tuple[socket.socket, int]:
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    for p in sorted(m._port_pool):
        try:
            s.bind(("127.0.0.1", p))
            s.listen()
            return s, p
        except OSError:
            continue
    s.close()
    raise AssertionError("no bindable pool port available for the test")


def test_port_is_listening_detects_bound_port():
    m = RuntimeManager()
    s, held = _occupy_a_pool_port(m)
    try:
        assert m._port_is_listening(held) is True
        # A port nobody is on reads as free.
        free_guess = max(m._port_pool) + 123
        assert m._port_is_listening(free_guess) is False
    finally:
        s.close()


def test_allocate_skips_os_held_port():
    m = RuntimeManager()
    s, held = _occupy_a_pool_port(m)
    try:
        got = m._allocate_port()
        assert got != held, "must not allocate a port an orphaned preview is listening on"
        assert not m._port_is_listening(got), "allocated port should actually be free"
    finally:
        s.close()
