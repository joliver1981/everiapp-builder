"""Tiny semver helpers for the marketplace publish flow.

The builder keeps auto-incrementing INTEGER snapshots (Save Version); a
public marketplace RELEASE gets a human-chosen semver at publish time. These
helpers back the publish dialog's bump buttons and the downgrade guard.
"""
from __future__ import annotations

import re

# Canonical semver only — no leading zeros (1.02.0), which compare numerically
# equal to 1.2.0 but would store/lookup as a distinct string (duplicate rows).
_SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def parse_semver(v: str) -> tuple[int, int, int] | None:
    m = _SEMVER_RE.match((v or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def is_valid_semver(v: str) -> bool:
    return parse_semver(v) is not None


def compare_semver(a: str, b: str) -> int:
    """-1 if a<b, 0 if equal, +1 if a>b. Unparseable sorts as 0.0.0."""
    pa, pb = parse_semver(a) or (0, 0, 0), parse_semver(b) or (0, 0, 0)
    return (pa > pb) - (pa < pb)


def bump_semver(v: str, part: str) -> str:
    major, minor, patch = parse_semver(v) or (0, 0, 0)
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"
