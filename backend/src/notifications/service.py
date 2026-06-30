"""Compose + send notification emails, and the per-event helpers."""
from __future__ import annotations

import asyncio
import logging
import smtplib
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..apps.models import App
from ..auth.models import User
from ..platform_settings.service import get_all

logger = logging.getLogger(__name__)


@dataclass
class NotifyResult:
    ok: bool
    error: str | None = None


def _smtp_send(host: str, port: int, use_tls: bool, username: str, password: str,
               from_addr: str, to_addrs: list[str], msg_bytes: bytes) -> None:
    """Synchronous SMTP send — call inside an executor. Monkeypatched in tests."""
    with smtplib.SMTP(host, port, timeout=15) as s:
        if use_tls:
            s.starttls()
        if username:
            s.login(username, password)
        s.sendmail(from_addr, to_addrs, msg_bytes)


async def send_email(db: AsyncSession, to: list[str], subject: str, body: str) -> NotifyResult:
    cfg = await get_all(db)
    if not cfg.get("smtp_enabled"):
        return NotifyResult(False, "smtp_disabled")
    host = (cfg.get("smtp_host") or "").strip()
    if not host:
        return NotifyResult(False, "no_host")
    recipients = [t.strip() for t in to if t and t.strip()]
    if not recipients:
        return NotifyResult(False, "no_recipients")

    from_addr = (cfg.get("notify_from") or cfg.get("smtp_username") or "aihub@localhost").strip()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body)

    try:
        await asyncio.get_event_loop().run_in_executor(
            None, _smtp_send,
            host, int(cfg.get("smtp_port") or 587), bool(cfg.get("smtp_use_tls", True)),
            cfg.get("smtp_username") or "", cfg.get("smtp_password") or "",
            from_addr, recipients, msg.as_bytes(),
        )
        return NotifyResult(True, None)
    except Exception as e:  # never propagate into the caller
        logger.warning("notification email failed: %s", e)
        return NotifyResult(False, str(e))


# --- Recipient resolution --------------------------------------------------
async def _admin_emails(db: AsyncSession, cfg: dict) -> list[str]:
    explicit = [e.strip() for e in (cfg.get("notify_admin_emails") or "").split(",") if e.strip()]
    if explicit:
        return explicit
    rows = (await db.execute(select(User).where(User.role == "admin"))).scalars().all()
    return [u.email for u in rows if u.email]


async def _app_name(db: AsyncSession, app_id: str) -> str:
    app = (await db.execute(select(App).where(App.id == app_id))).scalar_one_or_none()
    return app.name if app else app_id


async def _user_email(db: AsyncSession, user_id: str) -> str | None:
    u = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    return u.email if u and u.email else None


# --- Event helpers (best-effort) -------------------------------------------
async def notify_publish_requested(db: AsyncSession, app_id: str, requester_name: str,
                                   request_id: str) -> None:
    try:
        cfg = await get_all(db)
        if not cfg.get("smtp_enabled") or not cfg.get("notify_on_publish_request"):
            return
        name = await _app_name(db, app_id)
        await send_email(db, await _admin_emails(db, cfg),
                         f"[EveriApp] Publish request: {name}",
                         f"{requester_name} requested to publish '{name}'.\n\n"
                         f"Review it in EveriApp (request {request_id}).")
    except Exception:
        logger.exception("notify_publish_requested failed")


async def notify_publish_decided(db: AsyncSession, app_id: str, requester_id: str,
                                 decision: str, note: str = "") -> None:
    try:
        cfg = await get_all(db)
        if not cfg.get("smtp_enabled"):
            return
        email = await _user_email(db, requester_id)
        if not email:
            return
        name = await _app_name(db, app_id)
        body = f"Your request to publish '{name}' was {decision}."
        if note:
            body += f"\n\nReviewer note: {note}"
        await send_email(db, [email], f"[EveriApp] Publish {decision}: {name}", body)
    except Exception:
        logger.exception("notify_publish_decided failed")


async def notify_deploy_failed(db: AsyncSession, app_id: str, target_name: str,
                               error: str) -> None:
    try:
        cfg = await get_all(db)
        if not cfg.get("smtp_enabled") or not cfg.get("notify_on_deploy_failure"):
            return
        name = await _app_name(db, app_id)
        await send_email(db, await _admin_emails(db, cfg),
                         f"[EveriApp] Deploy failed: {name}",
                         f"Deployment of '{name}' to '{target_name}' failed:\n\n{error}")
    except Exception:
        logger.exception("notify_deploy_failed failed")


async def notify_bug_report(db: AsyncSession, app_id: str, summary: str) -> None:
    try:
        cfg = await get_all(db)
        if not cfg.get("smtp_enabled") or not cfg.get("notify_on_bug_report"):
            return
        name = await _app_name(db, app_id)
        await send_email(db, await _admin_emails(db, cfg),
                         f"[EveriApp] Bug report: {name}",
                         f"A bug was reported on '{name}':\n\n{summary}")
    except Exception:
        logger.exception("notify_bug_report failed")


# Budget-exceeded emails are throttled per user so a blocked user retrying in a
# loop can't flood admins. In-memory (resets on restart) — fine for a notice.
_BUDGET_NOTIFY_THROTTLE: dict[str, float] = {}
_BUDGET_NOTIFY_INTERVAL_S = 3600.0


async def notify_budget_exceeded(db: AsyncSession, user_id: str, reason: str) -> None:
    try:
        cfg = await get_all(db)
        if not cfg.get("smtp_enabled") or not cfg.get("notify_on_budget"):
            return
        now = time.monotonic()
        if now - _BUDGET_NOTIFY_THROTTLE.get(user_id, 0.0) < _BUDGET_NOTIFY_INTERVAL_S:
            return
        _BUDGET_NOTIFY_THROTTLE[user_id] = now
        u = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        who = u.username if u else user_id
        await send_email(db, await _admin_emails(db, cfg),
                         "[EveriApp] LLM budget exceeded",
                         f"The LLM budget was exceeded for user '{who}'.\n\n{reason}")
    except Exception:
        logger.exception("notify_budget_exceeded failed")
