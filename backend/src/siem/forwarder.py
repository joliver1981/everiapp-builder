"""SIEM forwarding core: cursor-tracked tail of audit_logs + push transports."""
from __future__ import annotations

import asyncio
import json
import logging
import socket
from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..platform_settings.service import get_all, get_setting, set_setting
from ..secrets.models import AuditLog

logger = logging.getLogger(__name__)

_CURSOR_KEY = "siem_cursor"  # {"created_at": iso|None, "id": str|None}
_BATCH = 500


class SiemError(Exception):
    pass


def _event_dict(row: AuditLog) -> dict:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "action": row.action,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "details": row.details,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "source": "aihub",
    }


async def _get_cursor(db: AsyncSession) -> tuple[datetime | None, str | None]:
    cur = await get_setting(db, _CURSOR_KEY)
    if not isinstance(cur, dict):
        return None, None
    ts = cur.get("created_at")
    parsed = None
    if ts:
        try:
            parsed = datetime.fromisoformat(ts)
        except ValueError:
            parsed = None
    return parsed, cur.get("id")


async def _set_cursor(db: AsyncSession, row: AuditLog) -> None:
    await set_setting(db, _CURSOR_KEY, {
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "id": row.id,
    })


async def _pending_query(db: AsyncSession, limit: int | None = _BATCH):
    ts, last_id = await _get_cursor(db)
    q = select(AuditLog)
    if ts is not None:
        q = q.where(or_(
            AuditLog.created_at > ts,
            and_(AuditLog.created_at == ts, AuditLog.id > (last_id or "")),
        ))
    q = q.order_by(AuditLog.created_at, AuditLog.id)
    if limit:
        q = q.limit(limit)
    return list((await db.execute(q)).scalars().all())


async def pending_count(db: AsyncSession) -> int:
    ts, last_id = await _get_cursor(db)
    q = select(func.count(AuditLog.id))
    if ts is not None:
        q = q.where(or_(
            AuditLog.created_at > ts,
            and_(AuditLog.created_at == ts, AuditLog.id > (last_id or "")),
        ))
    return int((await db.execute(q)).scalar_one() or 0)


# --- Transports ------------------------------------------------------------
async def _push_http(endpoint: str, auth_header: str, events: list[dict]) -> None:
    """POST newline-delimited JSON (one event per line) to an HTTP collector."""
    import httpx

    headers = {"Content-Type": "application/x-ndjson"}
    if auth_header:
        # Accept either "Header: value" or a bare token (assumed Authorization).
        if ":" in auth_header:
            name, _, value = auth_header.partition(":")
            headers[name.strip()] = value.strip()
        else:
            headers["Authorization"] = auth_header
    body = "\n".join(json.dumps(e) for e in events)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(endpoint, content=body, headers=headers)
        if resp.status_code >= 300:
            raise SiemError(f"SIEM HTTP push failed: {resp.status_code} {resp.text[:200]}")


def _syslog_send(host: str, port: int, events: list[dict]) -> None:
    """Send each event as an RFC3164-ish UDP syslog line. Blocking — call in a thread."""
    # facility=local0(16), severity=info(6) => PRI = 16*8+6 = 134
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for e in events:
            line = f"<134>aihub: {json.dumps(e)}"
            sock.sendto(line.encode("utf-8", errors="replace"), (host, port))
    finally:
        sock.close()


async def _push_syslog(endpoint: str, events: list[dict]) -> None:
    host, _, port_s = endpoint.partition(":")
    try:
        port = int(port_s) if port_s else 514
    except ValueError:
        raise SiemError(f"Invalid syslog endpoint '{endpoint}' (want host:port)")
    if not host:
        raise SiemError("Syslog endpoint missing host")
    await asyncio.get_event_loop().run_in_executor(None, _syslog_send, host, port, events)


async def push_events(db: AsyncSession, events: list[dict]) -> None:
    """Push a batch using the currently-configured transport. Raises on failure."""
    cfg = await get_all(db)
    endpoint = (cfg.get("siem_endpoint") or "").strip()
    transport = (cfg.get("siem_transport") or "http").strip().lower()
    auth_header = cfg.get("siem_auth_header") or ""
    if not endpoint:
        raise SiemError("No SIEM endpoint configured")
    if transport == "syslog":
        await _push_syslog(endpoint, events)
    else:
        await _push_http(endpoint, auth_header, events)


# --- Orchestration ---------------------------------------------------------
async def flush_once(db: AsyncSession) -> dict:
    """Forward one batch of pending audit events. Returns a summary dict."""
    cfg = await get_all(db)
    if not cfg.get("siem_enabled"):
        return {"forwarded": 0, "skipped": "disabled"}
    if not (cfg.get("siem_endpoint") or "").strip():
        return {"forwarded": 0, "skipped": "no_endpoint"}

    rows = await _pending_query(db)
    if not rows:
        return {"forwarded": 0}

    events = [_event_dict(r) for r in rows]
    await push_events(db, events)          # raises on failure — cursor NOT advanced
    await _set_cursor(db, rows[-1])        # only advance after a successful push
    return {"forwarded": len(rows), "transport": cfg.get("siem_transport", "http"),
            "endpoint": cfg.get("siem_endpoint")}


async def status(db: AsyncSession) -> dict:
    cfg = await get_all(db)
    ts, last_id = await _get_cursor(db)
    return {
        "enabled": bool(cfg.get("siem_enabled")),
        "endpoint": cfg.get("siem_endpoint") or "",
        "transport": cfg.get("siem_transport") or "http",
        "pending": await pending_count(db),
        "cursor": {"created_at": ts.isoformat() if ts else None, "id": last_id},
    }


async def siem_forwarder_loop():
    """Background loop: periodically flush new audit events to the SIEM."""
    from ..database import async_session

    await asyncio.sleep(20)  # let startup settle
    interval = max(5, settings.siem_flush_interval_seconds)
    while True:
        try:
            async with async_session() as db:
                cfg = await get_all(db)
                if cfg.get("siem_enabled") and (cfg.get("siem_endpoint") or "").strip():
                    # Drain in batches so a large backlog clears within one tick.
                    while True:
                        result = await flush_once(db)
                        if result.get("forwarded", 0) < _BATCH:
                            break
        except Exception:
            logger.exception("siem_forwarder: flush iteration failed")
        await asyncio.sleep(interval)
