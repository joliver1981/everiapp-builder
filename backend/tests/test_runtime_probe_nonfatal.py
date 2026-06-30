"""A runtime-probe INFRA crash (Playwright/serve/timeout) must NOT fail the build
or trigger the self-heal loop — the LLM can't fix a crashed probe, and the app
already passed tsc/build/boot. Real app errors still fail.
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "runtime-probe-test")

from src.ai.verifier import VerifyError, _build_runtime_result  # noqa: E402


def _e(msg: str, stage: str = "runtime") -> VerifyError:
    return VerifyError(stage=stage, file=None, line=None, column=None, code=None, message=msg)


def test_probe_crash_is_non_fatal_when_no_app_errors():
    r = _build_runtime_result([], "TimeoutError", 1.0, "app1")
    assert r.passed is True                      # build is NOT failed on infra
    assert "skipped" in r.summary.lower()
    assert "TimeoutError" in r.summary


def test_real_app_error_wins_over_probe_crash():
    r = _build_runtime_result([_e("console.error: boom")], "TimeoutError", 1.0, "app1")
    assert r.passed is False                     # a genuine app error still fails
    assert "boom" in r.errors[0].message


def test_app_error_fails_normally():
    r = _build_runtime_result([_e("Uncaught TypeError: x is undefined")], None, 1.0, "app1")
    assert r.passed is False


def test_clean_when_no_errors_and_no_crash():
    r = _build_runtime_result([], None, 1.0, "app1")
    assert r.passed is True
    assert r.summary == "runtime clean"


def test_duplicate_errors_are_deduped():
    r = _build_runtime_result([_e("same error"), _e("same error")], None, 1.0, "app1")
    assert len(r.errors) == 1
