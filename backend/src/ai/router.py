import logging
import time
from collections import defaultdict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db, async_session
from ..auth.service import auth_service
from ..auth.dependencies import get_current_user, require_role
from ..auth.models import User
from ..ai_providers.service import ai_provider_service
from .service import ai_service
from . import snapshots

logger = logging.getLogger(__name__)
router = APIRouter()

# Mounted at /api/apps — chat undo/rewind over the draft history ring buffer.
rewind_router = APIRouter()


@rewind_router.get("/{app_id}/history")
async def list_draft_history(
    app_id: str,
    _u: User = Depends(require_role("admin", "developer")),
):
    """List rewind points (newest first) captured before each AI turn."""
    return {"entries": snapshots.history_list(app_id)}


@rewind_router.post("/{app_id}/history/{seq}/restore")
async def restore_draft_history(
    app_id: str,
    seq: int,
    _u: User = Depends(require_role("admin", "developer")),
):
    """Rewind the draft to a prior turn. The current state is saved first, so
    the rewind itself can be undone."""
    if not snapshots.history_restore(app_id, seq):
        raise HTTPException(status_code=404, detail="History entry not found")
    return {"ok": True, "restored_seq": seq}

# Simple in-memory rate limiter: max messages per user per window
_RATE_LIMIT_MAX = 10        # messages
_RATE_LIMIT_WINDOW = 60     # seconds
_rate_buckets: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(user_id: str) -> bool:
    """Return True if request is allowed, False if rate-limited."""
    now = time.monotonic()
    bucket = _rate_buckets[user_id]
    _rate_buckets[user_id] = [t for t in bucket if now - t < _RATE_LIMIT_WINDOW]
    if len(_rate_buckets[user_id]) >= _RATE_LIMIT_MAX:
        return False
    _rate_buckets[user_id].append(now)
    return True


@router.get("/providers")
async def list_available_providers(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List available AI providers for any authenticated user (non-sensitive info only)."""
    providers = await ai_provider_service.list_providers(db)
    return [
        {
            "id": p.id,
            "name": p.name,
            "provider_type": p.provider_type,
            "default_model": p.default_model,
            "is_default_generation": p.is_default_generation,
        }
        for p in providers
        if p.is_active
    ]


@router.get("/conversations/{app_id}")
async def get_conversation_history(
    app_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get the latest conversation history for an app."""
    from sqlalchemy import select, desc
    from ..apps.models import Conversation, Message

    # Get the most recent conversation for this app
    result = await db.execute(
        select(Conversation)
        .where(Conversation.app_id == app_id)
        .order_by(desc(Conversation.created_at))
        .limit(1)
    )
    conversation = result.scalar_one_or_none()

    if not conversation:
        return {"conversation_id": None, "messages": []}

    # Get all messages for this conversation
    msg_result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at)
    )
    messages = msg_result.scalars().all()

    # Reconstruct the SAME display the live chat showed: strip the AI's [[jump:...]]
    # directives out of the prose and surface them as structured code_refs (the "jump to
    # code" chips at the bottom of a reply). Derived on read from the stored raw response,
    # so reloading a conversation looks identical to the live view — and conversations
    # created before chips existed get repaired too.
    from .code_parser import extract_jump_directives

    out_messages = []
    for msg in messages:
        content = msg.content or ""
        code_refs: list = []
        if msg.role == "assistant" and content:
            code_refs, content = extract_jump_directives(content)
        out_messages.append({
            "id": msg.id,
            "role": msg.role,
            "content": content,
            "code_refs": code_refs,
            "timestamp": msg.created_at.isoformat(),
        })

    return {"conversation_id": conversation.id, "messages": out_messages}


@router.websocket("/chat")
async def websocket_chat(websocket: WebSocket):
    await websocket.accept()

    # Authenticate via first message
    try:
        auth_msg = await websocket.receive_json()
        token = auth_msg.get("token")
        if not token:
            await websocket.send_json({"type": "error", "data": "Authentication required"})
            await websocket.close()
            return

        payload = auth_service.decode_access_token(token)
        if not payload:
            await websocket.send_json({"type": "error", "data": "Invalid token"})
            await websocket.close()
            return

        user_id = payload["sub"]
        await websocket.send_json({"type": "authenticated", "data": {"user_id": user_id}})
    except WebSocketDisconnect:
        return

    # Chat loop
    try:
        while True:
            data = await websocket.receive_json()
            app_id = data.get("app_id")
            message = data.get("message")
            conversation_id = data.get("conversation_id")
            provider_id = data.get("provider_id")  # optional: user-selected provider
            live_code = bool(data.get("live_code", False))  # watch the AI write files live
            editor_context = data.get("editor_context")  # what the user is viewing (in-code overlay)
            if not isinstance(editor_context, dict):
                editor_context = None

            if not app_id or not message:
                await websocket.send_json({"type": "error", "data": "app_id and message required"})
                continue

            # Rate limiting
            if not _check_rate_limit(user_id):
                await websocket.send_json({
                    "type": "error",
                    "data": f"Rate limited — max {_RATE_LIMIT_MAX} messages per minute. Please wait.",
                })
                continue

            # Stream response
            async with async_session() as db:
                async for chunk in ai_service.chat(db, app_id, message, conversation_id, provider_id, user_id=user_id, live_code=live_code, editor_context=editor_context):
                    await websocket.send_json(chunk)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.exception("WebSocket chat error for user %s", user_id)
        try:
            await websocket.send_json({"type": "error", "data": "An internal error occurred. Please try again."})
        except Exception:
            pass
