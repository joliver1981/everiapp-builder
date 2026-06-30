"""Admin endpoint to send a test email and verify SMTP config."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_role
from ..auth.models import User
from ..database import get_db
from .service import send_email

admin_router = APIRouter()


class TestEmailIn(BaseModel):
    to: str | None = None


@admin_router.post("/test")
async def send_test_email(
    body: TestEmailIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    to = (body.to or user.email or "").strip()
    if not to:
        raise HTTPException(status_code=400,
                            detail="No recipient — set your admin email or pass 'to'.")
    res = await send_email(db, [to], "EveriApp test email",
                           "This is a test email from EveriApp. If you received it, SMTP is configured correctly.")
    if not res.ok:
        raise HTTPException(status_code=502, detail=f"Send failed: {res.error}")
    return {"ok": True, "sent_to": to}
