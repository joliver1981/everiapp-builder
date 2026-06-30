"""Tests for the runtime-start progress streaming.

Two things matter:
  1. start_app() must return IMMEDIATELY (no blocking on npm install or vite).
  2. The returned AppProcess has phase + phase_detail + phase_started_at set so
     the frontend's polling shows real progress.

Why this matters: pre-refactor, start_app blocked the request for 30-60s on
first launch (npm install) and the UI showed an opaque "Starting..." spinner.
"""
import asyncio
import time

import pytest

from src.runtime.manager import AppProcess, runtime_manager


@pytest.mark.asyncio
async def test_start_app_returns_immediately(monkeypatch, tmp_path):
    """The HTTP request must not block on the npm install / vite spawn."""
    from src.config import settings
    monkeypatch.setattr(settings, "app_data_dir", str(tmp_path))

    # Create a fake draft dir so _do_start doesn't immediately error out.
    app_id = "fake-app-immediately"
    (tmp_path / app_id / "draft" / "frontend").mkdir(parents=True)

    # Force _do_start to sleep for 5s so we can verify start_app returned
    # before _do_start finished.
    started_at = time.monotonic()
    work_finished = asyncio.Event()

    async def slow_do_start(app_proc, source):
        try:
            app_proc.phase = "spawning"
            await asyncio.sleep(5)
            app_proc.status = "running"
        finally:
            work_finished.set()

    monkeypatch.setattr(runtime_manager, "_do_start", slow_do_start)

    proc = await runtime_manager.start_app(app_id, source="draft")
    elapsed = time.monotonic() - started_at

    assert elapsed < 1.0, f"start_app blocked for {elapsed:.2f}s — must be fire-and-return"
    assert proc.status == "starting"
    assert proc.phase == "queued"
    assert proc.phase_detail
    assert proc.phase_started_at > 0
    # Cleanup
    await work_finished.wait()
    await runtime_manager.stop_app(app_id)


@pytest.mark.asyncio
async def test_phase_updates_propagate(monkeypatch, tmp_path):
    """While _do_start runs, get_status() returns the latest phase."""
    from src.config import settings
    monkeypatch.setattr(settings, "app_data_dir", str(tmp_path))

    app_id = "fake-app-phases"
    (tmp_path / app_id / "draft" / "frontend").mkdir(parents=True)

    phase_log: list[str] = []

    async def stepped_do_start(app_proc, source):
        for phase, detail in [
            ("installing", "npm install in draft"),
            ("spawning", "starting vite"),
            ("waiting", "polling readiness"),
        ]:
            runtime_manager._set_phase(app_proc, phase, detail)
            phase_log.append(phase)
            # Give get_status() a chance to observe each phase
            await asyncio.sleep(0.05)
        app_proc.status = "running"
        runtime_manager._set_phase(app_proc, "running", "all set")

    monkeypatch.setattr(runtime_manager, "_do_start", stepped_do_start)
    proc = await runtime_manager.start_app(app_id, source="draft")
    assert proc.phase == "queued"

    # Poll until we see at least 2 different phases, then verify the latest
    seen_phases: set[str] = set()
    deadline = time.monotonic() + 3
    last_seen = None
    while time.monotonic() < deadline:
        s = runtime_manager.get_status(app_id)
        if s:
            seen_phases.add(s.phase)
            last_seen = s
        if last_seen and last_seen.phase == "running":
            break
        await asyncio.sleep(0.03)

    assert "installing" in seen_phases, f"phase 'installing' never observed, saw {seen_phases}"
    assert last_seen is not None and last_seen.phase == "running"
    assert last_seen.status == "running"
    await runtime_manager.stop_app(app_id)


@pytest.mark.asyncio
async def test_phase_started_at_drives_elapsed(monkeypatch, tmp_path):
    """phase_started_at is a monotonic timestamp the router converts to elapsed seconds."""
    from src.config import settings
    monkeypatch.setattr(settings, "app_data_dir", str(tmp_path))
    app_id = "fake-app-elapsed"
    (tmp_path / app_id / "draft" / "frontend").mkdir(parents=True)

    async def hold(app_proc, source):
        runtime_manager._set_phase(app_proc, "installing", "synthetic hold")
        await asyncio.sleep(2)
        app_proc.status = "running"

    monkeypatch.setattr(runtime_manager, "_do_start", hold)
    proc = await runtime_manager.start_app(app_id, source="draft")
    t_set = proc.phase_started_at
    await asyncio.sleep(0.8)
    elapsed = time.monotonic() - proc.phase_started_at
    assert 0.5 < elapsed < 2.5, f"elapsed wrong: {elapsed:.2f}"
    # phase_started_at should NOT change while still in the same phase
    assert proc.phase_started_at == t_set
    await runtime_manager.stop_app(app_id)
