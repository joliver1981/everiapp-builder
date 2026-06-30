"""Publish-gate evaluation on top of the static scanner."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ..platform_settings.service import get_setting
from .scanner import ScanReport, scan_app


class GateDecision:
    def __init__(self, *, report: ScanReport, blocked: bool, threshold: str,
                 enabled: bool, blocking: list):
        self.report = report
        self.blocked = blocked
        self.threshold = threshold
        self.enabled = enabled
        self.blocking = blocking

    def to_dict(self) -> dict:
        return {
            "blocked": self.blocked,
            "enabled": self.enabled,
            "threshold": self.threshold,
            "blocking_count": len(self.blocking),
            "blocking_findings": [f.to_dict() for f in self.blocking],
            "report": self.report.to_dict(),
        }


async def evaluate_publish_gate(db: AsyncSession, app_id: str) -> GateDecision:
    """Run the scan and decide whether publishing should be blocked.

    Controlled by three platform settings:
      * security_scan_enabled        — run the scan at all (default True)
      * security_scan_block_publish  — let findings block publish (default True)
      * security_scan_block_severity — the rung to block at (default "high")
    """
    enabled = bool(await get_setting(db, "security_scan_enabled"))
    block = bool(await get_setting(db, "security_scan_block_publish"))
    threshold = str(await get_setting(db, "security_scan_block_severity") or "high")

    report = scan_app(app_id)
    if not enabled:
        return GateDecision(report=report, blocked=False, threshold=threshold,
                            enabled=False, blocking=[])

    blocking = report.at_or_above(threshold)
    blocked = bool(block and blocking)
    return GateDecision(report=report, blocked=blocked, threshold=threshold,
                        enabled=True, blocking=blocking)
