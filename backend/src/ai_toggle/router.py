"""AI Toggle router — chat endpoint for in-app AI assistant."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user_flexible
from ..auth.models import User
from ..database import get_db
from ..apps.service import apps_service
from .schemas import ToggleChatRequest, ToggleChatResponse
from .service import ai_toggle_service

router = APIRouter()


@router.get("/{app_id}/status")
async def toggle_status(
    app_id: str,
    user: User = Depends(get_current_user_flexible),
    db: AsyncSession = Depends(get_db),
):
    """Get AI Toggle status for an app."""
    app = await apps_service.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="App not found")
    return {"enabled": app.ai_toggle_enabled, "app_id": app_id}


@router.post("/{app_id}/chat", response_model=ToggleChatResponse)
async def toggle_chat(
    app_id: str,
    body: ToggleChatRequest,
    user: User = Depends(get_current_user_flexible),
    db: AsyncSession = Depends(get_db),
):
    """Process a chat message from the in-app AI Toggle assistant."""
    app = await apps_service.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="App not found")

    if not app.ai_toggle_enabled:
        raise HTTPException(status_code=403, detail="AI Toggle is not enabled for this app")

    return await ai_toggle_service.chat(db, app_id, body)
