"""The admin master switch for the headless runtime probe.

verify_app(runtime_enabled=...) decides whether the (slow, browser-based)
runtime stage runs at all. Default off in production via the
`runtime_probe_enabled` platform setting; these tests pin the gate behavior
without spawning a real browser.
"""
from pathlib import Path

import pytest

from src.ai import verifier
from src.ai.verifier import VerifyResult, verify_app


def _passing(stage):
    async def _f(app_id, *a, **k):
        return VerifyResult(stage_reached=stage, summary=f"{stage} clean")
    return _f


@pytest.fixture
def _stub_early_stages(monkeypatch):
    """tsc / build / boot all pass so we reach the runtime gate quickly."""
    monkeypatch.setattr(verifier, "run_tsc", _passing("tsc"))
    monkeypatch.setattr(verifier, "run_build", _passing("build"))
    monkeypatch.setattr(verifier, "run_boot_probe", _passing("boot"))


@pytest.mark.asyncio
async def test_disabled_skips_the_probe(_stub_early_stages, monkeypatch):
    async def _boom(*a, **k):
        raise AssertionError("run_runtime_probe must NOT be called when disabled")
    monkeypatch.setattr(verifier, "run_runtime_probe", _boom)

    r = await verify_app("app1", "tsc_build_boot_runtime", runtime_enabled=False)
    assert r.passed is True
    assert r.stage_reached == "boot"
    assert "disabled by admin" in r.summary.lower()


@pytest.mark.asyncio
async def test_enabled_runs_the_probe(_stub_early_stages, monkeypatch):
    calls = {"n": 0, "a11y": None}

    async def _probe(app_id, run_a11y=False):
        calls["n"] += 1
        calls["a11y"] = run_a11y
        return VerifyResult(stage_reached="runtime", summary="runtime clean")
    monkeypatch.setattr(verifier, "run_runtime_probe", _probe)

    r = await verify_app("app1", "tsc_build_boot_runtime", runtime_enabled=True)
    assert calls["n"] == 1 and calls["a11y"] is False
    assert r.passed is True and r.stage_reached == "runtime"


@pytest.mark.asyncio
async def test_disabled_is_moot_below_runtime_level(_stub_early_stages, monkeypatch):
    async def _boom(*a, **k):
        raise AssertionError("probe should never run at tsc_build_boot")
    monkeypatch.setattr(verifier, "run_runtime_probe", _boom)

    r = await verify_app("app1", "tsc_build_boot", runtime_enabled=False)
    assert r.passed is True and r.stage_reached == "boot"


@pytest.mark.asyncio
async def test_default_runtime_enabled_is_true(_stub_early_stages, monkeypatch):
    """Omitting the flag keeps the old behavior (probe runs) — callers opt OUT."""
    ran = {"n": 0}

    async def _probe(app_id, run_a11y=False):
        ran["n"] += 1
        return VerifyResult(stage_reached="runtime", summary="ok")
    monkeypatch.setattr(verifier, "run_runtime_probe", _probe)

    await verify_app("app1", "tsc_build_boot_runtime")  # no runtime_enabled arg
    assert ran["n"] == 1


def test_probe_child_and_shared_module_present():
    """The out-of-process child + its dependency-free shared module must exist
    and be self-consistent (the child imports probe_shared as a sibling)."""
    from src.ai import probe_shared
    assert probe_shared.MOUNT_TIMEOUT_MS > 0
    assert "() =>" in probe_shared.A11Y_AUDIT_JS
    assert probe_shared.is_noise("[vite] connected.") is True
    assert probe_shared.is_noise("TypeError: boom") is False

    child = Path(verifier.__file__).with_name("runtime_probe_child.py")
    assert child.exists(), "runtime_probe_child.py missing next to verifier.py"
    src = child.read_text(encoding="utf-8")
    assert "import probe_shared" in src
    assert "async_playwright" in src


# ---------- The new out-of-process plumbing (pure functions) ----------


def test_parse_child_output_finds_json_among_noise():
    out = "npm warn whatever\n  some stderr leaked\n" + '{"mounted": true, "page_errors": []}'
    data = verifier._parse_child_output(out)
    assert data == {"mounted": True, "page_errors": []}


def test_parse_child_output_none_when_absent():
    assert verifier._parse_child_output("no json here\nat all") is None
    assert verifier._parse_child_output("") is None


def test_collect_runtime_errors_clean_when_mounted():
    data = {"mounted": True, "page_errors": [], "console_errors": [],
            "failed_requests": [], "a11y_raw": []}
    assert verifier._collect_runtime_errors(data, "http://x/") == []


def test_collect_runtime_errors_blank_page_synthesized():
    data = {"mounted": False, "page_errors": [], "console_errors": [],
            "failed_requests": [], "a11y_raw": []}
    errs = verifier._collect_runtime_errors(data, "http://x/")
    assert len(errs) == 1
    assert "#root has no children" in errs[0].message


def test_collect_runtime_errors_filters_noise_keeps_real():
    data = {
        "mounted": True,
        "page_errors": ["TypeError: Cannot read properties of undefined"],
        "console_errors": ["[vite] connected.", "boom happened"],
        "failed_requests": ["GET http://x/app.js — net::ERR"],
        "a11y_raw": [],
    }
    errs = verifier._collect_runtime_errors(data, "http://x/")
    msgs = [e.message for e in errs]
    assert "TypeError: Cannot read properties of undefined" in msgs
    assert "console.error: boom happened" in msgs
    assert any("network failure" in m for m in msgs)
    assert not any("[vite] connected" in m for m in msgs)  # noise stripped


def test_collect_runtime_errors_maps_a11y_findings():
    data = {"mounted": True, "page_errors": [], "console_errors": [],
            "failed_requests": [],
            "a11y_raw": [{"rule": "image-alt", "detail": "Image is missing an alt attribute",
                          "selector": "img.logo"}]}
    errs = verifier._collect_runtime_errors(data, "http://x/")
    assert any(e.code == "image-alt" for e in errs)
