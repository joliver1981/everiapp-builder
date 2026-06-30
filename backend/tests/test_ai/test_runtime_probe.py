"""Unit tests for the Playwright runtime-probe layer.

We don't spawn an actual browser here — that's covered by the manual smoke flow
on a live app. These tests cover the parts that have clear input/output
contracts: the noise filter and the verify_app() level handling.
"""
import pytest

from src.ai.verifier import (
    VERIFY_LEVELS,
    _RUNTIME_IGNORE_SUBSTRINGS,
    _is_noise,
    verify_app,
)


# ---------- Noise filter ----------


@pytest.mark.parametrize("msg", [
    "Download the React DevTools for a better development experience",
    "[vite] connecting...",
    "[vite] connected.",
    "[HMR] Waiting for update signal from WDS...",
    "[vite-plugin-react] reload",
    "Warning: ReactDOM.render is no longer supported in React 18",
])
def test_known_noise_is_filtered(msg):
    assert _is_noise(msg) is True


@pytest.mark.parametrize("msg", [
    "TypeError: Cannot read properties of undefined (reading 'map')",
    "Uncaught ReferenceError: foo is not defined",
    "Error: Invalid hook call. Hooks can only be called inside the body of a function component",
    "Failed to fetch /api/data: 500 Internal Server Error",
    "console.error: oops",
])
def test_real_errors_are_not_filtered(msg):
    assert _is_noise(msg) is False


def test_noise_filter_substrings_are_stable():
    """If someone trims the list down to nothing, we'd silently surface DevTools
    nags as 'errors'. Pin the size so that requires a deliberate update."""
    assert len(_RUNTIME_IGNORE_SUBSTRINGS) >= 5


# ---------- Level handling ----------


def test_runtime_level_is_in_valid_set():
    assert "tsc_build_boot_runtime" in VERIFY_LEVELS


def test_levels_are_an_ordered_progression():
    """Each level should be a strict superset of the previous one.

    The optional a11y stage runs after the runtime probe in the same browser
    session (see verifier.run_runtime_probe(run_a11y=...))."""
    expected = (
        "off", "tsc", "tsc_build", "tsc_build_boot",
        "tsc_build_boot_runtime", "tsc_build_boot_runtime_a11y",
    )
    assert VERIFY_LEVELS == expected


@pytest.mark.asyncio
async def test_verify_app_off_returns_disabled():
    """Level 'off' short-circuits before touching disk."""
    result = await verify_app("nonexistent-app-id", "off")
    assert result.passed is True
    assert result.summary == "verification disabled"
    assert result.stage_reached == "done"


@pytest.mark.asyncio
async def test_verify_app_unknown_level_returns_disabled():
    """Unknown levels are treated as 'off' rather than crashing."""
    result = await verify_app("nonexistent-app-id", "obviously-bogus-level")
    assert result.passed is True
    assert result.summary == "verification disabled"
