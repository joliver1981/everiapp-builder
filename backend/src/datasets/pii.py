"""PII redaction utilities for datasets.

Used by:
  - Preview rows  (admin preview never returns raw PII columns to the wire)
  - Runtime executor (when row passes through the runtime proxy, PII columns
                      are surfaced redacted unless the caller has explicit
                      `pii_view` permission, which we'll add later)
  - Audit log details writer (always redact)

The mapping is the dataset's `pii_tags` field: { column_name -> tag }
"""
from __future__ import annotations

from typing import Any, Iterable

REDACTED_PLACEHOLDER = "[REDACTED]"


def redact_row(row: dict[str, Any], pii_columns: Iterable[str]) -> dict[str, Any]:
    """Return a copy of `row` with PII columns replaced by the placeholder."""
    pii = set(pii_columns)
    return {k: (REDACTED_PLACEHOLDER if k in pii else v) for k, v in row.items()}


def redact_rows(rows: list[dict[str, Any]], pii_columns: Iterable[str]) -> list[dict[str, Any]]:
    pii = set(pii_columns)
    if not pii:
        return rows
    return [{k: (REDACTED_PLACEHOLDER if k in pii else v) for k, v in r.items()} for r in rows]


def pii_columns(pii_tags: dict[str, str] | None) -> list[str]:
    """Just the column names that are tagged."""
    return list((pii_tags or {}).keys())
