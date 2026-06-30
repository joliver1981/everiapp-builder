"""Risk threshold gating: which (risk_level, threshold) combos auto-approve.

This is the load-bearing safety check — a regression here means we'd auto-deploy
high-risk fixes the user didn't authorize.
"""
import pytest

from src.bug_reports.service import is_risk_within_threshold


@pytest.mark.parametrize(
    "risk,threshold,expected",
    [
        # threshold=none always blocks
        ("low", "none", False),
        ("medium", "none", False),
        ("high", "none", False),

        # threshold=low only auto-approves low
        ("low", "low", True),
        ("medium", "low", False),
        ("high", "low", False),

        # threshold=medium auto-approves low + medium, never high
        ("low", "medium", True),
        ("medium", "medium", True),
        ("high", "medium", False),

        # high is NEVER auto-approved, regardless of threshold
        ("high", "high", False),

        # Defensive: empty/garbage threshold = no auto-approval
        ("low", "", False),
        ("low", "garbage", False),
        ("garbage", "low", False),  # unknown risk treated as too risky
    ],
)
def test_threshold_gate(risk, threshold, expected):
    assert is_risk_within_threshold(risk, threshold) is expected
