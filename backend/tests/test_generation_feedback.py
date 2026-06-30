"""Workstream #1 — generation feedback + self-heal early-stop heuristics.

Covers the pure logic that stops the fix loop from churning 8x on an unfixable
problem and that surfaces a clear, actionable message (e.g. the 'no dataset
registered' case that produced 'Fixing 1/8 -> Runtime issue -> Fixing 2/8...').
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "gen-feedback-test")

from src.ai.prompts import NO_DATASETS_NOTICE, available_datasets_block  # noqa: E402
from src.ai.service import (  # noqa: E402
    _attach_config_guidance,
    _error_signature,
    classify_config_issue,
)
from src.ai.verifier import VerifyError, VerifyResult  # noqa: E402


def _err(stage: str, message: str) -> VerifyError:
    return VerifyError(stage=stage, file=None, line=None, column=None, code=None, message=message)


def test_no_datasets_notice_forbids_inventing_usedataset():
    low = NO_DATASETS_NOTICE.lower()
    assert "usedataset" in low
    assert "do not call" in low          # explicit: don't invent a dataset call
    assert "sample" in low               # instructs to use sample data instead
    # The populated-block helper still returns None for empty input; the AI
    # service substitutes the notice in that case.
    assert available_datasets_block([]) is None


def test_error_signature_detects_no_progress():
    a = [_err("runtime", "Cannot read properties of undefined (reading 'map')")]
    b = [_err("runtime", "Cannot read properties of undefined (reading 'map')")]
    c = [_err("tsc", "TS2304: Cannot find name 'foo'")]
    assert _error_signature(a) == _error_signature(b)   # identical => stuck, stop
    assert _error_signature(a) != _error_signature(c)   # changed => progress, keep going
    assert _error_signature([]) == _error_signature([])


def test_classify_config_issue_flags_dataset_failures_only():
    ds = [_err("runtime", "useDataset('sales') failed: 403 dataset not bound")]
    guidance = classify_config_issue(ds)
    assert guidance is not None
    assert "Admin → Datasets" in guidance
    # A plain code bug must NOT be classified as a config issue.
    assert classify_config_issue([_err("tsc", "TS2322: type mismatch")]) is None
    assert classify_config_issue([]) is None


def test_attach_config_guidance_prepends_actionable_note():
    res = VerifyResult(
        stage_reached="runtime", duration_seconds=1.0,
        errors=[_err("runtime", "useDataset failed: no such dataset")],
        summary="runtime error",
    )
    out = _attach_config_guidance(res)
    assert out.errors[0].stage == "config"
    assert "Datasets" in out.errors[0].message
    assert "data/config" in out.summary

    # A non-config failure is returned untouched.
    res2 = VerifyResult(
        stage_reached="tsc", duration_seconds=1.0,
        errors=[_err("tsc", "TS2304: Cannot find name 'x'")], summary="tsc error",
    )
    before = list(res2.errors)
    out2 = _attach_config_guidance(res2)
    assert out2.errors == before
    assert out2.summary == "tsc error"
