"""AI-drafted marketplace listing metadata — powers the publish dialog's Suggest button.

One-shot (non-streaming) completion following the ai_toggle/bug-analyzer pattern:
resolve provider config -> acompletion -> extract the JSON object -> clamp fields
to what the marketplace's publish schema accepts.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai_prompts import registry as prompt_registry
from ..ai_providers.service import ai_provider_service
from ..apps.models import App
from ..auth.models import User
from ..config import settings
from ..versions.service import versions_service
from .external import MarketplaceError
from .suggest_prompts import MARKETPLACE_CATEGORIES

logger = logging.getLogger(__name__)

_MAX_FILES = 8
_MAX_FILE_CHARS = 3_000
_MAX_DIFF_CHARS = 8_000
_PRIORITY_FILES = ("App.tsx", "main.tsx")


def _read_key_files(app_id: str) -> str:
    """A capped sample of the app's draft source, App.tsx first."""
    src = Path(settings.app_data_dir) / app_id / "draft" / "frontend" / "src"
    if not src.exists():
        return "(no source files found)"
    candidates = [p for p in sorted(src.rglob("*")) if p.is_file() and p.suffix in (".tsx", ".ts")]
    candidates.sort(key=lambda p: (p.name not in _PRIORITY_FILES, str(p)))
    blocks: list[str] = []
    for p in candidates[:_MAX_FILES]:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")[:_MAX_FILE_CHARS]
        except OSError:
            continue
        rel = p.relative_to(src).as_posix()
        blocks.append(f"### src/{rel}\n```tsx\n{text}\n```")
    return "\n\n".join(blocks) or "(no source files found)"


async def _diff_context(app_id: str, target_version: int) -> str:
    """Unified-diff summary of what changed in the version being published."""
    if target_version < 2:
        return ""
    try:
        diff = await versions_service.diff_versions(
            app_id, str(target_version - 1), str(target_version)
        )
    except ValueError:
        return ""
    parts: list[str] = [
        f"Files changed: {diff['summary']['modified']} modified, "
        f"{diff['summary']['added']} added, {diff['summary']['removed']} removed"
    ]
    remaining = _MAX_DIFF_CHARS
    for f in diff["files"]:
        if remaining <= 0:
            parts.append("… (more changes truncated)")
            break
        chunk = f"--- {f['path']} ({f['status']}) ---\n{f.get('diff') or ''}"[:remaining]
        parts.append(chunk)
        remaining -= len(chunk)
    return "\n".join(parts)


def _extract_json(raw: str) -> dict:
    """Parse the model's reply: strip fences, then first brace-balanced object."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = "\n".join(l for l in raw.splitlines() if not l.strip().startswith("```")).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(raw)):
            if raw[i] == "{":
                depth += 1
            elif raw[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(raw[start:i + 1])
                    except json.JSONDecodeError:
                        break
    raise MarketplaceError("The AI reply wasn't valid JSON — try again")


def _clamp(data: dict) -> dict:
    """Clamp fields to what the marketplace publish schema accepts."""
    short = str(data.get("short_description") or "").strip()[:300]
    category = str(data.get("category") or "general").strip().lower()
    if category not in MARKETPLACE_CATEGORIES:
        category = "general"
    tags = [str(t).strip().lower()[:50] for t in (data.get("tags") or []) if str(t).strip()][:10]
    bump = str(data.get("suggested_bump") or "").strip().lower()
    if bump not in ("patch", "minor", "major"):
        bump = "minor"
    return {
        "short_description": short,
        "description": str(data.get("description") or "").strip()[:50_000],
        "category": category,
        "tags": tags,
        "release_notes": str(data.get("release_notes") or "").strip()[:5_000],
        "setup_instructions": str(data.get("setup_instructions") or "").strip()[:20_000],
        "suggested_bump": bump,
    }


async def suggest_metadata(
    db: AsyncSession, app_id: str, user: User, *, version: int | None = None
) -> dict:
    """Draft listing metadata for the publish dialog. Returns the clamped fields."""
    app = (await db.execute(select(App).where(App.id == app_id))).scalar_one_or_none()
    if not app:
        raise MarketplaceError("App not found")

    # Budget gate (same policy as generation; never let bookkeeping break the call)
    try:
        from ..platform_settings.service import check_budget
        budget = await check_budget(db, user.id)
        if not budget.allowed:
            raise MarketplaceError(f"LLM budget exceeded: {budget.reason}")
    except MarketplaceError:
        raise
    except Exception:
        pass

    # Own purpose (inherits the generation default when unpinned) — matches the
    # purpose this call already records in llm_usage.
    provider_config = await ai_provider_service.get_default_provider_config(db, purpose="marketplace_metadata")
    if not provider_config:
        raise MarketplaceError("No AI provider configured. Ask an admin to set one up.")

    target_version = version or app.current_version
    system_prompt = await prompt_registry.resolve(db, "marketplace_metadata_prompt")

    wizard_note = (
        json.dumps(app.setup_wizard, indent=2)[:2_000] if app.setup_wizard else "(none)"
    )
    diff_text = await _diff_context(app_id, target_version)
    user_prompt = "\n\n".join(filter(None, [
        f"App name: {app.name}",
        f"Internal description: {app.description or '(none)'}",
        f"Version being published: {target_version}",
        f"Setup wizard schema:\n{wizard_note}",
        f"Diff for this version:\n{diff_text}" if diff_text else "",
        f"Key source files:\n{_read_key_files(app_id)}",
    ]))

    from ..llm_compat import acompletion

    provider_type = provider_config["provider_type"]
    model = provider_config["model"]
    llm_model = model if provider_type == "openai" else f"{provider_type}/{model}"

    response = await acompletion(
        model=llm_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        api_key=provider_config["api_key"],
        base_url=provider_config.get("base_url"),
        max_tokens=2_048,
        temperature=0.4,
    )
    raw = response.choices[0].message.content or ""

    # Cost metering (best-effort, same as generation)
    try:
        from ..llm_usage.service import record_usage
        usage = getattr(response, "usage", None)
        await record_usage(
            db,
            user_id=user.id,
            app_id=app_id,
            provider_type=provider_type,
            model=model,
            purpose="marketplace_metadata",
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or max(1, len(raw) // 4),
        )
    except Exception:
        pass

    return _clamp(_extract_json(raw))
