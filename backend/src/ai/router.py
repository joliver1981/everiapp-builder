import logging
import time
from collections import defaultdict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, HTTPException, File, Form, UploadFile, Response
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db, async_session
from ..auth.service import auth_service
from ..auth.dependencies import get_current_user, require_role
from ..auth.models import User
from ..ai_providers.service import ai_provider_service
from .service import ai_service
from . import snapshots
from .attachments import classify_upload, ACCEPTED_DESCRIPTION, meta as attachment_meta
from ..llm_compat import model_capabilities

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
    out = []
    for p in providers:
        if not p.is_active:
            continue
        # Input-modality hints for the attachment UI: True/False when litellm
        # knows the model, None for brand-new ids (the UI treats None as "try").
        caps = model_capabilities(p.provider_type, p.default_model)
        out.append({
            "id": p.id,
            "name": p.name,
            "provider_type": p.provider_type,
            "default_model": p.default_model,
            "is_default_generation": p.is_default_generation,
            "supports_vision": caps.get("vision"),
            "supports_pdf": caps.get("pdf"),
        })
    return out


# --- Chat attachments (screenshots, images, PDFs, text files) ---------------
# Files are uploaded over HTTP BEFORE the chat message is sent (multipart, no
# base64 inflation, no WebSocket frame-size ceiling), then referenced from the
# WS payload by id. Unbound uploads older than a day are pruned lazily.
_PENDING_ATTACHMENT_TTL_HOURS = 24


@router.post("/attachments")
async def upload_attachments(
    app_id: str = Form(...),
    files: list[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Store one or more chat attachments for `app_id`; returns their ids +
    metadata for the WS `attachment_ids` field. Unsupported types are refused
    (415) before anything is stored — nothing is ever silently dropped."""
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import delete
    from ..apps.models import App, MessageAttachment

    if await db.get(App, app_id) is None:
        raise HTTPException(status_code=404, detail="App not found")
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    classified = []
    for f in files:
        cls = classify_upload(f.filename, f.content_type)
        if cls is None:
            raise HTTPException(
                status_code=415,
                detail=f"'{f.filename or 'file'}' is not a supported attachment type. "
                       f"Supported: {ACCEPTED_DESCRIPTION}.",
            )
        classified.append((f, cls))

    rows: list[MessageAttachment] = []
    for f, (kind, mime) in classified:
        data = await f.read()
        if not data:
            raise HTTPException(status_code=400, detail=f"'{f.filename or 'file'}' is empty")
        rows.append(MessageAttachment(
            app_id=app_id, user_id=user.id,
            filename=(f.filename or f"attachment.{kind}")[:255],
            mime=mime, kind=kind, size=len(data), data=data,
        ))
        db.add(rows[-1])

    # Best-effort prune of uploads that were never sent.
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=_PENDING_ATTACHMENT_TTL_HOURS)
        await db.execute(delete(MessageAttachment).where(
            MessageAttachment.message_id.is_(None), MessageAttachment.created_at < cutoff))
    except Exception:
        logger.exception("pending-attachment prune failed (ignored)")

    await db.flush()  # ids are generated at flush
    await db.commit()
    return {"attachments": [attachment_meta(r) for r in rows]}


@router.get("/attachments/{attachment_id}")
async def get_attachment(
    attachment_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The stored bytes of an attachment (thumbnails / open-in-new-tab)."""
    from ..apps.models import MessageAttachment
    row = await db.get(MessageAttachment, attachment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    safe_name = "".join(ch for ch in (row.filename or "attachment") if ch >= " " and ch not in '"\\')
    return Response(
        content=row.data,
        media_type=row.mime or "application/octet-stream",
        headers={
            "Content-Disposition": f'inline; filename="{safe_name}"',
            "Cache-Control": "private, max-age=3600",
        },
    )


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

    # Attachment metadata (never the bytes) per message, one query.
    from ..apps.models import MessageAttachment
    att_by_msg: dict[str, list[dict]] = {}
    msg_ids = [m.id for m in messages if m.role == "user"]
    if msg_ids:
        rows = (await db.execute(
            select(MessageAttachment.id, MessageAttachment.message_id, MessageAttachment.filename,
                   MessageAttachment.mime, MessageAttachment.kind, MessageAttachment.size)
            .where(MessageAttachment.message_id.in_(msg_ids))
            .order_by(MessageAttachment.created_at)
        )).all()
        for r in rows:
            att_by_msg.setdefault(r.message_id, []).append(
                {"id": r.id, "name": r.filename, "mime": r.mime, "kind": r.kind, "size": r.size})

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
            "attachments": att_by_msg.get(msg.id, []),
            "timestamp": msg.created_at.isoformat(),
        })

    return {"conversation_id": conversation.id, "messages": out_messages}


@router.websocket("/chat")
async def websocket_chat(websocket: WebSocket):
    await websocket.accept()

    # Authenticate via first message. Failures send a TYPED auth_error payload
    # and close with an application code (4401) so clients can distinguish
    # "refresh your token and reconnect" from a normal close — the old bare
    # {"type":"error","data":"Invalid token"} + close(1000) forced clients to
    # string-match and surfaced verbatim in the builder chat.
    try:
        auth_msg = await websocket.receive_json()
        token = auth_msg.get("token")
        if not token:
            await websocket.send_json({"type": "auth_error", "data": {
                "code": "token_missing", "message": "Authentication required"}})
            await websocket.close(code=4401)
            return

        payload = auth_service.decode_access_token(token)
        if not payload:
            # Covers both expired and structurally-invalid tokens; the client
            # treats both identically (refresh + reconnect).
            await websocket.send_json({"type": "auth_error", "data": {
                "code": "token_invalid",
                "message": "Invalid or expired token — refresh and reconnect"}})
            await websocket.close(code=4401)
            return

        # Injected preview/embed session tokens are readable by generated-app
        # JS. The builder chat generates code and spends LLM budget — never
        # available to those.
        if payload.get("purpose") in ("preview", "embed"):
            await websocket.send_json({"type": "auth_error", "data": {
                "code": "token_scope",
                "message": "Preview/embed session tokens cannot use the builder chat"}})
            await websocket.close(code=4401)
            return

        user_id = payload["sub"]
        # The token only proves it WAS valid at mint time — check the account
        # still exists and is active before opening a generation channel.
        async with async_session() as db:
            user = await auth_service.get_user_by_id(db, user_id)
            if not user or not user.is_active:
                await websocket.send_json({"type": "auth_error", "data": {
                    "code": "account_disabled",
                    "message": "Your account has been deactivated"}})
                await websocket.close(code=4401)
                return
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
            # Ids from POST /api/ai/attachments (screenshots, PDFs, text files).
            attachment_ids = data.get("attachment_ids")
            if not isinstance(attachment_ids, list):
                attachment_ids = []
            attachment_ids = [str(i) for i in attachment_ids if isinstance(i, (str, int))]
            if message is None:
                message = ""
            if not isinstance(message, str):
                message = str(message)

            # A message may be attachments-only ("what's wrong with this screenshot?"
            # is often just the screenshot), but never empty.
            if not app_id or (not message.strip() and not attachment_ids):
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
                # Re-validate the account per message: the socket authenticated
                # once at connect (tokens aren't re-checked on a live socket),
                # so without this a user deactivated by an admin keeps an open
                # chat generating apps indefinitely. One PK lookup per message
                # is noise next to the LLM call it gates.
                user = await auth_service.get_user_by_id(db, user_id)
                if not user or not user.is_active:
                    await websocket.send_json({"type": "auth_error", "data": {
                        "code": "account_disabled",
                        "message": "Your account has been deactivated"}})
                    await websocket.close(code=4401)
                    return
                async for chunk in ai_service.chat(db, app_id, message, conversation_id, provider_id, user_id=user_id, live_code=live_code, editor_context=editor_context, attachment_ids=attachment_ids):
                    await websocket.send_json(chunk)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.exception("WebSocket chat error for user %s", user_id)
        try:
            await websocket.send_json({"type": "error", "data": "An internal error occurred. Please try again."})
        except Exception:
            pass
