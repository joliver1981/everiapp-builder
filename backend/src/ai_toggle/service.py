"""AI Toggle service — processes chat messages for in-app AI assistant."""
import json
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from ..ai_providers.service import ai_provider_service
from .prompts import build_toggle_prompt
from .schemas import ToggleChatRequest, ToggleChatResponse, ActionCommand

logger = logging.getLogger(__name__)


class AIToggleService:
    async def chat(
        self,
        db: AsyncSession,
        app_id: str,
        request: ToggleChatRequest,
        user_id: str | None = None,
    ) -> ToggleChatResponse:
        """Process a chat message and return a response with optional actions."""
        # Build prompt with context
        system_prompt = build_toggle_prompt(
            data_sources=[ds.model_dump() for ds in request.context.dataSources],
            available_actions=request.context.availableActions,
        )

        # Purpose resolution falls back toggle-pin -> legacy toggle default ->
        # generation default -> first active, so one call covers every config.
        provider_config = await ai_provider_service.get_default_provider_config(db, purpose="toggle")
        if not provider_config:
            return ToggleChatResponse(
                response="No AI provider configured. Please ask an admin to set one up.",
            )

        try:
            from ..llm_compat import acompletion

            provider_type = provider_config["provider_type"]
            model = provider_config["model"]
            llm_model = model if provider_type == "openai" else f"{provider_type}/{model}"

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": request.message},
            ]

            response = await acompletion(
                model=llm_model,
                messages=messages,
                api_key=provider_config["api_key"],
                base_url=provider_config.get("base_url"),
                max_tokens=2048,
                temperature=0.5,
            )

            raw_content = response.choices[0].message.content or ""

            # Cost meter (best-effort, same pattern as generation) — this call
            # path used to be entirely invisible in llm_usage.
            try:
                from ..llm_usage.service import record_usage
                usage = getattr(response, "usage", None)
                await record_usage(
                    db,
                    user_id=user_id or "(unknown)",
                    app_id=app_id,
                    provider_type=provider_type,
                    model=model,
                    purpose="ai_toggle",
                    input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                    output_tokens=getattr(usage, "completion_tokens", 0) or max(1, len(raw_content) // 4),
                )
            except Exception:
                # A failed commit leaves the session pending-rollback; restore
                # it so the request can still finish.
                try:
                    await db.rollback()
                except Exception:
                    pass

            # Parse JSON response
            return self._parse_response(raw_content)

        except Exception as e:
            logger.exception("AI Toggle chat error for app %s", app_id)
            try:
                from ..llm_usage.service import record_usage
                await record_usage(
                    db,
                    user_id=user_id or "(unknown)",
                    app_id=app_id,
                    provider_type=provider_config.get("provider_type") or "",
                    model=provider_config.get("model") or "",
                    purpose="ai_toggle",
                    input_tokens=0,
                    output_tokens=0,
                    error=f"{type(e).__name__}: {e}",
                )
            except Exception:
                try:
                    await db.rollback()
                except Exception:
                    pass
            return ToggleChatResponse(
                response=f"Sorry, I encountered an error: {str(e)}",
            )

    def _parse_response(self, raw: str) -> ToggleChatResponse:
        """Parse the LLM's JSON response into a ToggleChatResponse."""
        # Try to extract JSON from the response
        raw = raw.strip()

        # Handle markdown code blocks
        if raw.startswith("```"):
            lines = raw.split("\n")
            # Remove first and last lines (``` markers)
            lines = [l for l in lines if not l.strip().startswith("```")]
            raw = "\n".join(lines).strip()

        try:
            data = json.loads(raw)
            response_text = data.get("response", raw)
            actions = []
            for a in data.get("actions", []):
                if isinstance(a, dict) and "name" in a:
                    actions.append(ActionCommand(name=a["name"], params=a.get("params", {})))
            return ToggleChatResponse(response=response_text, actions=actions)
        except Exception:
            # Any parse-shaped failure degrades to plain text: a JSON array
            # (AttributeError on .get), a non-dict params / non-string name
            # (pydantic ValidationError), etc. Raising here would make the
            # caller record a second llm_usage row for the same call.
            return ToggleChatResponse(response=raw)


ai_toggle_service = AIToggleService()
