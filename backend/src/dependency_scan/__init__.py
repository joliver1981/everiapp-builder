"""Dependency-upgrade scanner for generated apps.

Reads an app's package.json and flags dependencies that are unpinned, deprecated,
or below a known-safe version (from a small bundled advisory list — no live npm
registry needed, which matters for offline on-prem installs).
"""
from .scanner import scan_dependencies, DepFinding  # noqa: F401
