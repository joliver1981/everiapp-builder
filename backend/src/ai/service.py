import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ..config import settings
from ..apps.models import App, Conversation, Message
from ..ai_providers.service import ai_provider_service
from .prompts import SYSTEM_PROMPT, CONTINUATION_PROMPT, available_datasets_block, NO_DATASETS_NOTICE, JUMP_DIRECTIVE_PROMPT
from ..datasets.service import datasets_service
from .code_parser import parse_llm_response, extract_jump_directives, GeneratedFile
from .wizard_prompts import is_wizard_request, WIZARD_GENERATION_PROMPT
from . import snapshots
from .verifier import VerifyResult, VerifyError, errors_to_prompt_block, verify_app
from ..llm_compat import acompletion
from ..ai_prompts import registry as prompt_registry
from . import debug_log


# --- Self-heal early-stop heuristics (pure; unit-tested in test_generation_feedback) ---
def _error_signature(errors) -> frozenset:
    """A hashable fingerprint of a verify pass's errors — used to detect a fix
    attempt that left the EXACT same errors (i.e. made no progress)."""
    sig = set()
    for e in errors or []:
        msg = (getattr(e, "message", "") or "")[:120].strip().lower()
        sig.add((getattr(e, "stage", "") or "", msg))
    return frozenset(sig)


def classify_config_issue(errors) -> str | None:
    """If the failure looks like a DATA/CONFIG gap the code-fixer can't resolve
    (the app uses a dataset that isn't registered/bound), return an actionable
    message for the user; otherwise None."""
    for e in errors or []:
        msg = (getattr(e, "message", "") or "").lower()
        if "dataset" in msg or "usedataset" in msg:
            return (
                "This looks like a data/configuration issue, not a code bug: the app is "
                "trying to use a dataset that isn't attached to it yet. Attach it from the "
                "Data panel (the database icon in the builder top bar → 'Attach') — or, if it "
                "doesn't exist yet, create it first in Admin → Datasets and then attach it — "
                "then ask me to regenerate. Meanwhile I can rebuild with clearly-labeled "
                "sample data so the app still runs."
            )
    return None


def _attach_config_guidance(result: VerifyResult) -> VerifyResult:
    """If the result's errors indicate a config/data gap, surface a clear,
    actionable note at the top of the error list + summary."""
    guidance = classify_config_issue(result.errors)
    if guidance:
        result.errors = [VerifyError(
            stage="config", file=None, line=None, column=None, code=None, message=guidance,
        )] + list(result.errors)
        result.summary = "Stopped — likely a data/config issue, not a code bug."
    return result


# --- Smart prose/code stream splitter (pure; unit-tested in test_mid_stream_parser) ---
#
# The LLM streams prose interleaved with ```fenced``` file blocks whose first line is
# `// FILE: <path>`. We forward prose to the chat as `text` events (this is the original
# behaviour — code-block interiors are suppressed from the chat bubble) AND, when the
# builder asked to watch live, emit `code_stream` events so a side panel can show the file
# being written line-by-line.
#
# CRITICAL INVARIANT: this splitter NEVER touches `full_response`. The caller accumulates
# the raw stream verbatim, and that raw text is the single source of truth for
# parse_llm_response(). The splitter only decides what to *display*.

_FILE_HEADER_RE = re.compile(r'^\s*//\s*FILE:\s*(\S+)')


class _StreamState:
    """Cursor for `_smart_stream_events`, persisted across stream chunks.

    Kept in the caller's outer scope because a fence or `// FILE:` header can be split
    across `delta.content` chunk boundaries during token streaming.
    """
    __slots__ = ("phase", "fence_buffer", "line_buffer", "pending_path", "consumed_lang_line")

    def __init__(self):
        self.phase = "prose"             # prose | header | body
        self.fence_buffer = ""           # rolling 3-char tail for ``` detection
        self.line_buffer = ""            # current in-block line being accumulated
        self.pending_path = None         # path of the file currently streaming (body)
        self.consumed_lang_line = False  # have we eaten the ```lang line yet (header)


def _smart_stream_events(text: str, st: "_StreamState"):
    """Yield ('text', str) and/or ('code_stream', dict) events for one chunk of stream.

    State lives in `st` so the machine survives chunk boundaries. Prose `text` output is
    byte-identical to the original suppress-the-code loop; `code_stream` events are purely
    additive (and the caller drops them when the user isn't watching live).
    """
    for ch in text:
        if st.phase == "prose":
            st.fence_buffer += ch
            if st.fence_buffer.endswith("```"):
                # Entering a code block — same blank-line placeholder the old loop emitted.
                st.phase = "header"
                st.fence_buffer = ""
                st.line_buffer = ""
                st.pending_path = None
                st.consumed_lang_line = False
                yield ("text", "\n\n")
            elif len(st.fence_buffer) > 3:
                safe = st.fence_buffer[:-3]
                st.fence_buffer = st.fence_buffer[-3:]
                yield ("text", safe)

        elif st.phase == "header":
            if ch == "\n":
                line, st.line_buffer = st.line_buffer, ""
                m = _FILE_HEADER_RE.match(line)
                if m:
                    st.pending_path = m.group(1).strip()
                    st.phase = "body"
                    yield ("code_stream", {"event": "file_start", "path": st.pending_path})
                elif not st.consumed_lang_line:
                    # First line after ``` is the language tag (e.g. "tsx" or ""). Eat it
                    # and keep looking for the `// FILE:` header on the next line.
                    st.consumed_lang_line = True
                else:
                    # No FILE header — a headerless/example block (```bash, ```typescript).
                    # parse_llm_response ignores these, so we emit no code_stream, but still
                    # consume the block so prose resumes correctly after it closes.
                    st.pending_path = None
                    st.phase = "body"
            else:
                st.line_buffer += ch

        else:  # body
            st.line_buffer += ch
            st.fence_buffer = (st.fence_buffer + ch)[-3:]
            if st.fence_buffer == "```":
                # Closing fence. Strip the ``` we just accumulated from the line buffer and
                # flush any partial trailing line before ending the file.
                tail = st.line_buffer[:-3]
                st.line_buffer = ""
                st.fence_buffer = ""
                if st.pending_path is not None:
                    if tail:
                        yield ("code_stream", {"event": "delta", "path": st.pending_path, "text": tail})
                    yield ("code_stream", {"event": "file_end", "path": st.pending_path})
                st.phase = "prose"
                st.pending_path = None
            elif ch == "\n":
                line, st.line_buffer = st.line_buffer, ""
                if st.pending_path is not None:
                    yield ("code_stream", {"event": "delta", "path": st.pending_path, "text": line})


def _flush_stream_events(st: "_StreamState"):
    """Emit anything left in the buffers at end-of-stream.

    Mirrors the original loop's trailing `if code_fence_buffer and not in_code_block`
    flush, and additionally closes out an unterminated code block (truncated output) so a
    live panel doesn't hang on a perpetual "writing…".
    """
    if st.phase == "prose":
        if st.fence_buffer:
            yield ("text", st.fence_buffer)
            st.fence_buffer = ""
    elif st.pending_path is not None:
        tail = st.line_buffer
        if tail:
            yield ("code_stream", {"event": "delta", "path": st.pending_path, "text": tail})
        yield ("code_stream", {"event": "file_end", "path": st.pending_path})
    st.line_buffer = ""


# --- Editor-context formatter (pure; unit-tested in test_editor_context) ---
def _format_editor_context(ctx: dict | None) -> str:
    """Render the in-code overlay's editor context as a focused system message.

    `ctx` is the JSON the overlay sends: {path, selectionText, selStartLine, selEndLine,
    viewportStartLine, viewportEndLine}. Returns "" when there's nothing useful to say, so the
    caller can `if block:` before appending. The full file content is already available to the
    model via continuation context — this only POINTS at what's on screen + the selection.
    """
    if not isinstance(ctx, dict):
        return ""
    path = (ctx.get("path") or "").strip()
    if not path:
        return ""

    lines: list[str] = ["## What the user is looking at right now"]

    vstart, vend = ctx.get("viewportStartLine"), ctx.get("viewportEndLine")
    if isinstance(vstart, int) and isinstance(vend, int) and vend >= vstart:
        lines.append(f"They are viewing `{path}` in the code editor (lines {vstart}-{vend} on screen).")
    else:
        lines.append(f"They are viewing `{path}` in the code editor.")

    sel = (ctx.get("selectionText") or "").strip()
    sstart, send = ctx.get("selStartLine"), ctx.get("selEndLine")
    if sel:
        snippet = sel[:4000]
        if len(sel) > 4000:
            snippet += "\n… (selection truncated)"
        where = ""
        if isinstance(sstart, int) and isinstance(send, int):
            where = f" (lines {sstart}-{send})" if send != sstart else f" (line {sstart})"
        lines.append(
            f"They have SELECTED this code{where} — focus your help on this selection unless they ask otherwise:"
        )
        lines.append("```")
        lines.append(snippet)
        lines.append("```")
    else:
        lines.append("Assume their question is about this file / the on-screen region unless they say otherwise.")

    return "\n".join(lines)


class AIService:
    async def chat(self, db: AsyncSession, app_id: str, user_message: str, conversation_id: str | None = None, provider_id: str | None = None, user_id: str | None = None, live_code: bool = False, editor_context: dict | None = None):
        """Process a chat message and yield streaming response chunks.

        Yields dicts with keys: type ('status', 'text', 'files', 'code_stream',
        'error', 'done'), data.

        `user_id` (when provided) drives LLM budget enforcement + usage recording.
        `live_code` (when True) additionally streams `code_stream` events so the builder
        can show the AI writing each file live; harmless to leave off.
        `editor_context` (when provided, from the in-code overlay) tells the model exactly
        which file/lines the user is looking at + their selection, so it focuses there.
        """
        # Get or create conversation
        if conversation_id:
            result = await db.execute(
                select(Conversation).where(Conversation.id == conversation_id)
            )
            conversation = result.scalar_one_or_none()
        else:
            conversation = None

        if not conversation:
            conversation = Conversation(app_id=app_id, title=user_message[:100])
            db.add(conversation)
            await db.flush()

        # Save user message
        user_msg = Message(
            conversation_id=conversation.id,
            role="user",
            content=user_message,
        )
        db.add(user_msg)
        await db.flush()

        # Build messages for LLM (editor_context focuses the model on what the user is viewing)
        messages = await self._build_messages(db, conversation, app_id, editor_context=editor_context)

        # Get provider config — use selected provider or fall back to default
        if provider_id:
            provider_config = await ai_provider_service.get_provider_config(db, provider_id)
        else:
            provider_config = await ai_provider_service.get_default_provider_config(db, purpose="generation")
        if not provider_config:
            yield {"type": "error", "data": "No AI provider configured. Please add one in Admin > AI Providers."}
            return

        # --- Budget enforcement ------------------------------------------
        # Block if the user (or org) has blown the monthly LLM budget. Surface
        # a near-limit warning so the UI can show a banner.
        if user_id:
            try:
                from ..platform_settings.service import check_budget
                budget = await check_budget(db, user_id)
                if not budget.allowed:
                    yield {"type": "error", "data": f"LLM budget exceeded: {budget.reason}"}
                    try:
                        from ..notifications.service import notify_budget_exceeded
                        await notify_budget_exceeded(db, user_id, budget.reason)
                    except Exception:
                        pass
                    return
                if budget.near_limit:
                    yield {"type": "budget_warning", "data": budget.to_dict()}
            except Exception:
                pass  # never let budget bookkeeping break generation

        # Call LLM with streaming
        trace = None  # generation trace (full traceability), persisted at the end
        try:
            import litellm

            provider_type = provider_config["provider_type"]
            model = provider_config["model"]

            # litellm model format. openai is the bare model name; everything
            # else (anthropic, google, ollama, ...) uses "<provider>/<model>".
            if provider_type == "openai":
                llm_model = model
            else:
                llm_model = f"{provider_type}/{model}"

            # --- Build the generation trace (full traceability) --------------
            from ..generation_trace.service import TraceBuilder
            _sys_prompts = [m["content"] for m in messages if m.get("role") == "system"]
            trace = TraceBuilder(
                app_id=app_id, user_id=user_id, user_message=user_message,
                system_prompts=_sys_prompts, model=model, provider=provider_type,
                conversation_id=conversation.id,
            )
            trace.step(type="context", system_prompt_count=len(_sys_prompts),
                       message_count=len(messages), model=llm_model)
            debug_log.log("turn_start", app_id=app_id, conversation_id=conversation.id,
                          user_message=user_message, model=llm_model, system_prompts=_sys_prompts)

            full_response = ""
            _usage_in = 0
            _usage_out = 0

            response = await acompletion(
                model=llm_model,
                messages=messages,
                api_key=provider_config["api_key"],
                base_url=provider_config.get("base_url"),
                max_tokens=16384,
                temperature=0.7,
                stream=True,
                stream_options={"include_usage": True},
            )

            # Stream prose to the chat (code-block interiors stay suppressed there); when
            # the user is watching live, also emit code_stream events. The splitter never
            # feeds full_response — that stays the raw source of truth for parsing.
            st = _StreamState()

            async for chunk in response:
                # Usage arrives on the final chunk when include_usage is set.
                _u = getattr(chunk, "usage", None)
                if _u is not None:
                    _usage_in = getattr(_u, "prompt_tokens", 0) or _usage_in
                    _usage_out = getattr(_u, "completion_tokens", 0) or _usage_out
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta.content:
                    full_response += delta.content
                    for kind, payload in _smart_stream_events(delta.content, st):
                        if kind == "code_stream" and not live_code:
                            continue
                        yield {"type": kind, "data": payload}

            # Flush trailing prose / close an unterminated block.
            for kind, payload in _flush_stream_events(st):
                if kind == "code_stream" and not live_code:
                    continue
                yield {"type": kind, "data": payload}

            # Parse the complete response for files and wizard
            files, description, wizard = parse_llm_response(full_response)
            # Pull any [[jump:...]] directives out of the prose so the builder can
            # auto-navigate + highlight the referenced code; strip them from the
            # visible description (never from raw file bodies).
            code_refs, description = extract_jump_directives(description or "")
            debug_log.log("generated", app_id=app_id, description=(description or "")[:1000],
                          raw_response=debug_log.raw(full_response),
                          files=debug_log.files_payload(files))

            if trace is not None:
                trace.set_files([{"path": f.path, "action": f.action} for f in files])
                trace.step(type="generate", files=[f.path for f in files],
                           description=(description or "")[:500], response_chars=len(full_response))

            # Look up app-level verify config before touching files. We do this
            # BEFORE applying so that if anything's about to change, we can
            # snapshot the current state first (LKG = "last known good").
            result = await db.execute(select(App).where(App.id == app_id))
            app_for_verify = result.scalar_one_or_none()
            verify_level = (app_for_verify.ai_verify_level if app_for_verify else "off")
            max_iters = (app_for_verify.ai_verify_max_iterations if app_for_verify else 0)

            if files:
                # Push the pre-change draft onto the rewind history (independent
                # of verify level) so the user can undo/rewind any turn.
                try:
                    snapshots.history_push(app_id, note=user_message[:200])
                except Exception:
                    logger.exception("history_push failed for %s", app_id)
                # Take a snapshot of the pre-change draft so the user can roll back.
                if verify_level != "off":
                    snapshots.snapshot(app_id, note=user_message[:200])

                # Save files to disk
                await self._save_generated_files(app_id, files)
                yield {
                    "type": "files",
                    "data": [{"path": f.path, "action": f.action} for f in files],
                }

            # Self-heal loop: if verification fails, ask the LLM to fix and re-verify
            # until green, until we hit max_iters, or until the LLM stops producing files.
            final_verify: VerifyResult | None = None
            if files and verify_level != "off":
                # Platform-wide admin master switch for the headless runtime probe.
                from ..platform_settings.service import get_setting
                runtime_enabled = bool(await get_setting(db, "runtime_probe_enabled"))
                async for ev in self._self_heal_loop(
                    db, conversation, app_id, full_response, verify_level, max_iters,
                    provider_config, runtime_enabled, live_code, user_id=user_id,
                ):
                    if ev["type"] == "_final_verify":
                        final_verify = ev["data"]
                    else:
                        if trace is not None:
                            if ev["type"] == "verify_iteration":
                                d = ev.get("data") or {}
                                trace.step(type="verify", iteration=d.get("iteration"),
                                           passed=d.get("passed"), stage=d.get("stage"),
                                           summary=d.get("summary"), errors=d.get("errors", []),
                                           duration_seconds=d.get("duration_seconds"))
                                trace.iterations = max(trace.iterations, d.get("iteration") or 0)
                            elif ev["type"] == "files":
                                trace.step(type="fix",
                                           files=[f.get("path") for f in (ev.get("data") or [])])
                        yield ev

            # Save wizard schema if generated — but never an invalid one: a bad
            # stored schema breaks setup-status/setup for viewers and blocks
            # every subsequent manual save (PUT re-validates the whole document).
            if wizard:
                from ..apps.service import validate_wizard
                wizard_errors = validate_wizard(wizard)
                if wizard_errors:
                    logger.warning("Discarding invalid AI-generated wizard for %s: %s",
                                   app_id, "; ".join(wizard_errors))
                    yield {"type": "wizard_invalid", "data": {"errors": wizard_errors}}
                else:
                    result = await db.execute(select(App).where(App.id == app_id))
                    app = result.scalar_one_or_none()
                    if app:
                        app.setup_wizard = wizard
                    yield {"type": "wizard", "data": wizard}

            # Save assistant message (full raw response for history context)
            assistant_msg = Message(
                conversation_id=conversation.id,
                role="assistant",
                content=full_response,
                files_changed=[{"path": f.path, "action": f.action} for f in files] if files else None,
            )
            db.add(assistant_msg)
            await db.commit()

            # Record LLM usage for the cost meter. Best-effort — never break
            # the response if bookkeeping fails.
            try:
                from ..llm_usage.service import record_usage
                # Fallback estimate when the provider didn't return usage:
                # ~4 chars/token is the rough English heuristic.
                if not _usage_out:
                    _usage_out = max(1, len(full_response) // 4)
                await record_usage(
                    db,
                    user_id=user_id or "(unknown)",
                    app_id=app_id,
                    provider_type=provider_type,
                    model=model,
                    purpose="generation",
                    input_tokens=_usage_in,
                    output_tokens=_usage_out,
                )
            except Exception:
                pass

            # Send clean description to replace the streamed text
            done_data = {
                "conversation_id": conversation.id,
                "files_changed": len(files),
                "description": description,
                "wizard_generated": wizard is not None,
                # Code locations the AI pointed at (from [[jump:...]] directives) — the
                # builder turns these into clickable chips + (when enabled) an auto-jump.
                "code_refs": code_refs,
                # Verification outcome the UI uses to decide whether to show
                # the "Roll back to last-known-good" button.
                "verify": final_verify.to_dict() if final_verify else None,
                "rollback_available": (
                    final_verify is not None
                    and not final_verify.passed
                    and snapshots.has_snapshot(app_id)
                ),
            }
            # Persist the generation trace (full traceability).
            if trace is not None:
                try:
                    if final_verify is not None:
                        trace.finalize("passed" if final_verify.passed else "failed",
                                       summary=final_verify.summary, verify=final_verify.to_dict())
                    elif not files:
                        trace.finalize("no_files", summary=(description or "No file changes")[:500])
                    else:
                        trace.finalize("no_verify", summary="Generated (verification off)")
                    trace.step(type="done", files_changed=len(files))
                    await trace.save(db)
                except Exception:
                    logger.exception("generation trace save failed for %s", app_id)

            yield {"type": "done", "data": done_data}

        except Exception as e:
            logger.exception("AI chat error for app %s", app_id)
            if trace is not None:
                try:
                    trace.finalize("error", summary=str(e)[:500])
                    trace.step(type="error", message=str(e)[:500])
                    await trace.save(db)
                except Exception:
                    logger.exception("generation trace (error path) save failed for %s", app_id)
            # Don't leak internal details (e.g. API key errors from litellm)
            msg = str(e)
            if "api_key" in msg.lower() or "authentication" in msg.lower():
                msg = "AI provider authentication failed. Check your API key in Admin > AI Providers."
            yield {"type": "error", "data": msg}

    async def _build_messages(self, db: AsyncSession, conversation: Conversation, app_id: str, editor_context: dict | None = None) -> list[dict]:
        """Build the message list for the LLM call.

        `editor_context` (from the in-code overlay) is injected as a focused system message so
        the model knows exactly which file/lines the user is viewing + their selection.
        """
        messages = [{"role": "system", "content": await prompt_registry.resolve(db, "system_prompt")}]

        # Inject the org's custom system prompt (brand colors, component
        # conventions, house style) if an admin has configured one.
        try:
            from ..platform_settings.service import get_setting
            custom = await get_setting(db, "custom_system_prompt")
            if custom and str(custom).strip():
                messages.append({
                    "role": "system",
                    "content": "## Organization Conventions\n" + str(custom).strip(),
                })
        except Exception:
            pass

        # Teach the [[jump:...]] directive as its own system message so it stays active
        # even when an admin has overridden the main system_prompt in the prompt registry.
        messages.append({"role": "system", "content": JUMP_DIRECTIVE_PROMPT})

        # Focus the model on exactly what the user is looking at in the editor (from the
        # in-code collaboration overlay) — the open file, the on-screen lines, and any selection.
        _ctx_block = _format_editor_context(editor_context)
        if _ctx_block:
            messages.append({"role": "system", "content": _ctx_block})

        # Inject any bound datasets so the model can `useDataset()` them instead
        # of inventing sample data. Only datasets explicitly bound to this app
        # show up — keeps the prompt small and predictable.
        try:
            bound = await datasets_service.list_bindings(db, app_id)
            block = available_datasets_block(bound)
            # Always inject a data-sources block: the real one when datasets are
            # bound, otherwise an explicit "no datasets — use sample data" notice
            # so the model doesn't invent a useDataset() call that fails at runtime.
            messages.append({"role": "system", "content": block or await prompt_registry.resolve(db, "no_datasets_notice")})
        except Exception:
            # Never let a dataset-prompt failure break code generation.
            logger.exception("Failed to load bound datasets for AI prompt; continuing without")

        # Check if there are existing files (continuation)
        app_dir = Path(settings.app_data_dir) / app_id / "draft" / "frontend"
        current_files = self._read_current_files(app_dir)

        if current_files:
            _cont = await prompt_registry.resolve(db, "continuation_prompt")
            # Use replace (not .format) so an override containing literal { } (code
            # snippets) can't raise, and one without the token still gets the files.
            continuation_content = (
                _cont.replace("{current_files}", current_files)
                if "{current_files}" in _cont
                else f"{_cont}\n\n{current_files}"
            )
            messages.append({"role": "system", "content": continuation_content})

        # Add conversation history (last 20 messages to stay within context)
        result = await db.execute(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at)
        )
        history = result.scalars().all()

        for msg in history[-20:]:
            messages.append({"role": msg.role, "content": msg.content})

        # Check if the latest user message is a wizard request — augment prompt
        if history and is_wizard_request(history[-1].content):
            messages.insert(1, {"role": "system", "content": await prompt_registry.resolve(db, "wizard_generation_prompt")})

        return messages

    def _read_current_files(self, app_dir: Path) -> str:
        """Read current app files for context."""
        if not app_dir.exists():
            return ""

        src_dir = app_dir / "src"
        if not src_dir.exists():
            return ""

        files_content = []
        for root, _dirs, files in os.walk(src_dir):
            for file in files:
                if file.endswith(('.tsx', '.ts', '.css')) and 'node_modules' not in root:
                    filepath = Path(root) / file
                    rel_path = filepath.relative_to(app_dir)
                    try:
                        content = filepath.read_text(encoding='utf-8')
                        files_content.append(f"### {rel_path}\n```\n{content}\n```")
                    except Exception:
                        continue

        return "\n\n".join(files_content)

    async def _self_heal_loop(
        self,
        db: AsyncSession,
        conversation: Conversation,
        app_id: str,
        original_response: str,
        verify_level: str,
        max_iters: int,
        provider_config: dict,
        runtime_enabled: bool = True,
        live_code: bool = False,
        user_id: str | None = None,
    ):
        """Verify the just-applied files, and on red, ask the LLM to fix.

        Yields chat events: `verifying`, `verify_iteration`, `text`, `files`,
        `code_stream` (when `live_code`), plus a sentinel `_final_verify` event
        consumed by the outer scope.
        """
        import litellm

        # Initial verify pass
        yield {"type": "verifying", "data": {"level": verify_level, "iteration": 0, "max": max_iters}}
        result = await verify_app(app_id, verify_level, runtime_enabled)
        yield {"type": "verify_iteration", "data": {
            "iteration": 0,
            "passed": result.passed,
            "stage": result.stage_reached,
            "summary": result.summary,
            "errors": [e.message[:300] for e in result.errors[:5]],
            "duration_seconds": round(result.duration_seconds, 2),
        }}
        debug_log.log("verify", app_id=app_id, iteration=0, passed=result.passed,
                      stage=result.stage_reached, summary=result.summary,
                      errors=debug_log.errors_payload(result.errors))

        if result.passed:
            yield {"type": "_final_verify", "data": result}
            return

        last_response = original_response
        prev_sig = _error_signature(result.errors)
        for iteration in range(1, max_iters + 1):
            # Build a fix request: original conversation context + concrete errors.
            messages = await self._build_messages(db, conversation, app_id)
            messages.append({"role": "assistant", "content": last_response})
            _errors_block = errors_to_prompt_block(result.errors)
            messages.append({"role": "user", "content": _errors_block})

            provider_type = provider_config["provider_type"]
            model = provider_config["model"]
            llm_model = model if provider_type == "openai" else f"{provider_type}/{model}"

            try:
                fix_response = await acompletion(
                    model=llm_model,
                    messages=messages,
                    api_key=provider_config["api_key"],
                    base_url=provider_config.get("base_url"),
                    max_tokens=8192,
                    temperature=0.2,
                    stream=False,
                    aihub_span={"app_id": app_id, "user_id": user_id,
                                "purpose": "self_heal",
                                "provider_type": provider_type, "model": model},
                )
                raw = fix_response.choices[0].message.content or ""
            except Exception as e:
                logger.exception("self-heal LLM call failed at iteration %d", iteration)
                try:
                    from ..llm_usage.service import record_usage
                    # commit=False: this session still holds the turn's pending
                    # rows (user Message); the row rides the turn's transaction
                    # and lands with the outer assistant-message commit.
                    await record_usage(
                        db, user_id=user_id or "(unknown)", app_id=app_id,
                        provider_type=provider_type, model=model, purpose="self_heal",
                        input_tokens=0, output_tokens=0,
                        error=f"{type(e).__name__}: {e}",
                        commit=False,
                    )
                except Exception:
                    pass
                result.summary = f"{result.summary} — fix call failed: {e}"
                yield {"type": "_final_verify", "data": result}
                return

            # Cost meter: fix iterations burn real tokens — attribute them
            # separately from the main turn (purpose="self_heal") so the cost
            # dashboard can show how much healing costs per app. commit=False:
            # committing here would flush the turn's pending rows early, and a
            # failed commit would poison the shared session; the row rides the
            # turn's transaction instead (next commit is a few lines below).
            try:
                from ..llm_usage.service import record_usage
                _u = getattr(fix_response, "usage", None)
                await record_usage(
                    db, user_id=user_id or "(unknown)", app_id=app_id,
                    provider_type=provider_type, model=model, purpose="self_heal",
                    input_tokens=getattr(_u, "prompt_tokens", 0) or 0,
                    output_tokens=getattr(_u, "completion_tokens", 0) or max(1, len(raw) // 4),
                    commit=False,
                )
            except Exception:
                pass

            fix_files, _fix_desc, _fix_wizard = parse_llm_response(raw)
            last_response = raw
            debug_log.log("fix", app_id=app_id, iteration=iteration,
                          errors_fed=debug_log.raw(_errors_block),
                          raw_response=debug_log.raw(raw),
                          files=debug_log.files_payload(fix_files))

            if not fix_files:
                # LLM didn't propose any file changes — nothing to retry.
                yield {"type": "verify_iteration", "data": {
                    "iteration": iteration,
                    "passed": False,
                    "stage": "fix",
                    "summary": "LLM produced no file changes; stopping",
                    "errors": [],
                }}
                yield {"type": "_final_verify", "data": _attach_config_guidance(result)}
                return

            await self._save_generated_files(app_id, fix_files)
            # When watching live, replay each fixed file into the Live panel. The fix call
            # is non-streamed, so this is coarse (whole-file) rather than token-by-token.
            if live_code:
                for f in fix_files:
                    yield {"type": "code_stream", "data": {"event": "file_start", "path": f.path, "iteration": iteration}}
                    if f.content:
                        yield {"type": "code_stream", "data": {"event": "delta", "path": f.path, "text": f.content}}
                    yield {"type": "code_stream", "data": {"event": "file_end", "path": f.path}}
            yield {"type": "files", "data": [
                {"path": f.path, "action": f.action} for f in fix_files
            ]}

            # Persist this fix attempt as an assistant turn so future context sees it.
            db.add(Message(
                conversation_id=conversation.id,
                role="assistant",
                content=raw,
                files_changed=[{"path": f.path, "action": f.action} for f in fix_files],
            ))
            await db.commit()

            yield {"type": "verifying", "data": {
                "level": verify_level, "iteration": iteration, "max": max_iters,
            }}
            result = await verify_app(app_id, verify_level, runtime_enabled)
            yield {"type": "verify_iteration", "data": {
                "iteration": iteration,
                "passed": result.passed,
                "stage": result.stage_reached,
                "summary": result.summary,
                "errors": [e.message[:300] for e in result.errors[:5]],
                "duration_seconds": round(result.duration_seconds, 2),
            }}
            debug_log.log("verify", app_id=app_id, iteration=iteration, passed=result.passed,
                          stage=result.stage_reached, summary=result.summary,
                          errors=debug_log.errors_payload(result.errors))

            if result.passed:
                yield {"type": "_final_verify", "data": result}
                return

            new_sig = _error_signature(result.errors)
            if new_sig == prev_sig:
                # The fix didn't change the errors — we're stuck. Stop instead of
                # burning the remaining iterations on the identical failure.
                result.summary = (
                    f"No change after fix attempt {iteration} — stopping early to avoid "
                    f"wasted iterations. {result.summary}"
                )
                yield {"type": "verify_iteration", "data": {
                    "iteration": iteration,
                    "passed": False,
                    "stage": "stopped",
                    "summary": result.summary,
                    "errors": [e.message[:300] for e in result.errors[:5]],
                }}
                yield {"type": "_final_verify", "data": _attach_config_guidance(result)}
                return
            prev_sig = new_sig

        # Exhausted iterations.
        yield {"type": "_final_verify", "data": _attach_config_guidance(result)}

    async def _save_generated_files(self, app_id: str, files: list[GeneratedFile]) -> None:
        """Save generated files to the app's draft directory."""
        app_dir = Path(settings.app_data_dir) / app_id / "draft" / "frontend"
        app_dir.mkdir(parents=True, exist_ok=True)
        resolved_app_dir = app_dir.resolve()

        for f in files:
            file_path = (app_dir / f.path).resolve()
            # Path traversal guard: ensure resolved path stays inside app dir
            if not str(file_path).startswith(str(resolved_app_dir)):
                logger.warning("Path traversal blocked: %s (app %s)", f.path, app_id)
                continue

            if f.action == "delete":
                if file_path.exists():
                    file_path.unlink()
                continue

            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(f.content, encoding='utf-8')

        # decisions.json manifest → upsert the app's decision registry. Own
        # session (this method has none) and best-effort: a malformed manifest
        # must never break the generation turn — it logs and the file stays on
        # disk for the next turn to fix.
        # Canonical location is top-level, but tolerate src/decisions.json —
        # models sometimes follow the src/ allowlist habit.
        manifest_file = next((f for f in files
                              if f.path in ("decisions.json", "src/decisions.json")
                              and f.action != "delete"), None)
        if manifest_file is not None:
            try:
                import json as _json
                entries = _json.loads(manifest_file.content)
                if not isinstance(entries, list):
                    raise ValueError("decisions.json must be a JSON array")
                from ..database import async_session
                from ..decisions.service import upsert_from_manifest
                async with async_session() as mdb:
                    written = await upsert_from_manifest(mdb, app_id, entries)
                logger.info("decisions.json upserted %d decisions for %s: %s",
                            len(written), app_id, ", ".join(written))
            except Exception as e:
                logger.warning("decisions.json manifest rejected for %s: %s", app_id, e)


ai_service = AIService()
