"""Static security scanner for AI-generated app code + the publish gate.

The LLM that writes apps occasionally emits genuinely dangerous code — a
hardcoded API key, an `eval`, SQL built by string interpolation. This module
scans an app's draft source with a low-false-positive rule set and (optionally)
blocks publishing when a finding meets the configured severity threshold.
"""
from .scanner import scan_app, ScanReport, Finding, SEVERITY_ORDER  # noqa: F401
