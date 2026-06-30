"""Parse package.json and apply a bundled advisory rule-set.

Deliberately offline + explainable (same philosophy as security_scan): a small,
curated list of real advisories for common packages, plus structural rules
(unpinned ranges, deprecated packages). Not a substitute for `npm audit` against
a live registry, but it catches the high-signal issues with zero network/deps.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from ..config import settings

# package -> (min_safe_version, advisory). Versions below this are flagged 'high'.
# Curated from well-known advisories; extend as needed.
_MIN_SAFE: dict[str, tuple[str, str]] = {
    "lodash": ("4.17.21", "Prototype pollution (CVE-2021-23337)"),
    "axios": ("1.6.0", "SSRF / DoS fixes; upgrade to >= 1.6.0"),
    "minimist": ("1.2.6", "Prototype pollution (CVE-2021-44906)"),
    "node-fetch": ("2.6.7", "Exposure of sensitive information (CVE-2022-0235)"),
    "follow-redirects": ("1.15.4", "Improper input handling / info leak"),
    "json5": ("2.2.2", "Prototype pollution (CVE-2022-46175)"),
    "semver": ("7.5.2", "ReDoS (CVE-2022-25883)"),
    "ws": ("8.17.1", "DoS via crafted headers (CVE-2024-37890)"),
    "postcss": ("8.4.31", "Line-return parsing ReDoS (CVE-2023-44270)"),
    "vite": ("4.5.2", "Multiple dev-server fixes; upgrade Vite"),
    "braces": ("3.0.3", "Uncontrolled resource consumption (CVE-2024-4068)"),
}

# Packages that are deprecated / should be replaced.
_DEPRECATED: dict[str, str] = {
    "request": "Deprecated since 2020 — use fetch / axios / got.",
    "left-pad": "Trivial package; use String.prototype.padStart.",
    "moment": "In maintenance mode — prefer date-fns / Day.js / Temporal.",
}

_LOOSE = {"", "*", "latest", "x", "X"}
_SEMVER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


@dataclass
class DepFinding:
    package: str
    current: str
    severity: str       # low | medium | high
    issue: str
    recommendation: str

    def to_dict(self) -> dict:
        return {
            "package": self.package, "current": self.current, "severity": self.severity,
            "issue": self.issue, "recommendation": self.recommendation,
        }


def _parse_version(spec: str) -> tuple[int, int, int] | None:
    m = _SEMVER_RE.search(spec or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _below(current: str, minimum: str) -> bool:
    cv, mv = _parse_version(current), _parse_version(minimum)
    if cv is None or mv is None:
        return False
    return cv < mv


def _is_loose(spec: str) -> bool:
    s = (spec or "").strip()
    if s in _LOOSE:
        return True
    # Open-ended lower bound with no pin, e.g. ">=1.0.0" or "1.x"
    if s.startswith(">=") or s.endswith(".x") or s.endswith(".X"):
        return True
    return False


def scan_text(package_json_text: str) -> list[DepFinding]:
    try:
        data = json.loads(package_json_text)
    except json.JSONDecodeError:
        return []
    findings: list[DepFinding] = []
    deps: dict[str, str] = {}
    for section in ("dependencies", "devDependencies"):
        if isinstance(data.get(section), dict):
            deps.update({k: str(v) for k, v in data[section].items()})

    for name, spec in sorted(deps.items()):
        if name in _DEPRECATED:
            findings.append(DepFinding(name, spec, "medium",
                                       "Deprecated package", _DEPRECATED[name]))
        if name in _MIN_SAFE:
            minimum, advisory = _MIN_SAFE[name]
            if _below(spec, minimum):
                findings.append(DepFinding(name, spec, "high", advisory,
                                           f"Upgrade {name} to >= {minimum}"))
        if _is_loose(spec):
            findings.append(DepFinding(name, spec, "low", "Unpinned version range",
                                       "Pin to an exact or caret version for reproducible installs"))
    # Worst first.
    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (order.get(f.severity, 3), f.package))
    return findings


def scan_dependencies(app_id: str) -> dict:
    pkg = Path(settings.app_data_dir) / app_id / "draft" / "frontend" / "package.json"
    if not pkg.is_file():
        return {"package_json_found": False, "finding_count": 0, "findings": [], "counts": {}}
    try:
        text = pkg.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {"package_json_found": False, "finding_count": 0, "findings": [], "counts": {}}

    findings = scan_text(text)
    counts = {"high": 0, "medium": 0, "low": 0}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return {
        "package_json_found": True,
        "finding_count": len(findings),
        "counts": counts,
        "findings": [f.to_dict() for f in findings],
    }
