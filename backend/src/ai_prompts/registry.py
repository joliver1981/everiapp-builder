"""Registry of the backend AI prompts + the generation/verify FLOW they plug into.

Powers the admin "AI Prompts & Flow" panel:
  - VISIBILITY: every system prompt the platform sends to the LLM, with its exact
    default text and the pipeline stage it's used in.
  - CONTROL: admins override any prompt (stored in platform_settings under
    `prompt_override.<key>`). Generation/self-heal/bug-fix all read the *effective*
    text via resolve(), so an override genuinely takes effect — there are no dead
    settings. Reset clears the override back to the built-in default.
  - FLOW: a structured description of the pipeline stages so the UI can render a
    visual diagram and attach each prompt to the stage(s) that use it.

Overrides are powerful and risky (a bad system prompt breaks ALL app generation),
so the admin UI warns + audits every change.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from ..platform_settings.service import get_setting, set_setting

OVERRIDE_PREFIX = "prompt_override."


@dataclass(frozen=True)
class PromptDef:
    key: str
    title: str
    description: str
    stage: str  # FLOW_STAGES id this prompt is used in


# The prompts an admin can see + override. Defaults are resolved lazily (the
# constants live in other modules) to avoid import cycles.
PROMPTS: tuple[PromptDef, ...] = (
    PromptDef(
        "system_prompt", "App generation — system prompt",
        "Core instructions the AI follows to generate or modify an app's React/TypeScript "
        "code. The single most impactful prompt.",
        "generate",
    ),
    PromptDef(
        "continuation_prompt", "App modification — continuation",
        "Appended when modifying an existing app; carries the current files as context so "
        "edits are incremental rather than rewrites.",
        "generate",
    ),
    PromptDef(
        "no_datasets_notice", "No-datasets guidance",
        "Injected when the app has no datasets bound, so the AI renders labeled sample data "
        "instead of inventing a useDataset() call that fails at runtime.",
        "context",
    ),
    PromptDef(
        "wizard_generation_prompt", "Setup-wizard generation",
        "Used when the user asks for a marketplace setup-wizard schema for the app.",
        "generate",
    ),
    PromptDef(
        "analyzer_system_prompt", "Bug-report analyzer",
        "Instructions for the AI that triages an incoming bug report on a deployed app and "
        "proposes a fix.",
        "bug_fix",
    ),
    PromptDef(
        "marketplace_metadata_prompt", "Marketplace listing suggestions",
        "Drafts the marketplace listing (short description, category, tags, release notes, "
        "setup instructions) when a developer clicks Suggest in the publish dialog.",
        "publish",
    ),
)

_PROMPT_BY_KEY = {p.key: p for p in PROMPTS}


def _defaults() -> dict[str, str]:
    """Built-in default text for each prompt (imported lazily to avoid import cycles)."""
    from ..ai.prompts import SYSTEM_PROMPT, CONTINUATION_PROMPT, NO_DATASETS_NOTICE
    from ..ai.wizard_prompts import WIZARD_GENERATION_PROMPT
    from ..bug_reports.prompts import ANALYZER_SYSTEM_PROMPT
    from ..marketplace.suggest_prompts import SUGGEST_METADATA_PROMPT
    return {
        "system_prompt": SYSTEM_PROMPT,
        "continuation_prompt": CONTINUATION_PROMPT,
        "no_datasets_notice": NO_DATASETS_NOTICE,
        "wizard_generation_prompt": WIZARD_GENERATION_PROMPT,
        "analyzer_system_prompt": ANALYZER_SYSTEM_PROMPT,
        "marketplace_metadata_prompt": SUGGEST_METADATA_PROMPT,
    }


# The pipeline stages, in order. The UI renders this as a visual flow; each stage
# lists the prompts that plug into it (joined from PROMPTS by `stage`).
FLOW_STAGES: tuple[dict, ...] = (
    {
        "id": "context", "title": "Gather context", "icon": "database", "order": 1,
        "description": "Assemble the conversation, the app's current files, and any bound "
                       "datasets — or the no-datasets notice when none are bound.",
        "inputs": ["user message", "current files", "bound datasets"],
        "outputs": ["prompt messages"],
    },
    {
        "id": "generate", "title": "Generate code", "icon": "sparkles", "order": 2,
        "description": "The LLM writes or edits the app's files from the system prompt + the "
                       "user's request.",
        "inputs": ["prompt messages"], "outputs": ["generated files"],
    },
    {
        "id": "verify", "title": "Verify", "icon": "shield-check", "order": 3,
        "description": "tsc → vite build → boot probe → runtime probe (headless Chromium) → "
                       "optional a11y. Produces concrete, file-level errors.",
        "inputs": ["generated files"], "outputs": ["verify result (pass or errors)"],
    },
    {
        "id": "self_heal", "title": "Self-heal loop", "icon": "refresh-cw", "order": 4,
        "description": "On failure, the concrete errors are fed back to the LLM to fix, then "
                       "re-verified — up to N iterations. Stops early on no-progress or a "
                       "data/config issue.",
        "inputs": ["verify errors", "current files"], "outputs": ["fixed files"],
    },
    {
        "id": "bug_fix", "title": "Bug-report auto-fix", "icon": "bug", "order": 5,
        "description": "Separately: a deployed app's bug report is triaged by the AI, which "
                       "proposes a fix the same way.",
        "inputs": ["bug report"], "outputs": ["proposed fix"],
    },
    {
        "id": "publish", "title": "Marketplace publish", "icon": "upload", "order": 6,
        "description": "Separately: when publishing to the marketplace, the AI can draft the "
                       "listing metadata (description, tags, release notes, setup instructions) "
                       "from the app's code and version diff.",
        "inputs": ["app files", "version diff"], "outputs": ["listing metadata draft"],
    },
)


async def resolve(db: AsyncSession, key: str) -> str:
    """Effective prompt text: the admin override if set + non-empty, else the default."""
    defaults = _defaults()
    if key not in defaults:
        raise KeyError(f"unknown prompt key: {key}")
    override = await get_setting(db, OVERRIDE_PREFIX + key)
    if isinstance(override, str) and override.strip():
        return override
    return defaults[key]


async def catalog(db: AsyncSession) -> list[dict]:
    """Every overridable prompt with its default, current override, and effective text."""
    defaults = _defaults()
    out: list[dict] = []
    for p in PROMPTS:
        override = await get_setting(db, OVERRIDE_PREFIX + p.key)
        has = isinstance(override, str) and override.strip() != ""
        out.append({
            "key": p.key, "title": p.title, "description": p.description, "stage": p.stage,
            "default": defaults[p.key],
            "override": override if has else None,
            "is_overridden": has,
            "effective": override if has else defaults[p.key],
        })
    return out


def flow() -> list[dict]:
    """Pipeline stages with the prompts that plug into each — for the visual panel."""
    by_stage: dict[str, list[dict]] = {}
    for p in PROMPTS:
        by_stage.setdefault(p.stage, []).append({"key": p.key, "title": p.title})
    return [{**s, "prompts": by_stage.get(s["id"], [])} for s in FLOW_STAGES]


async def set_override(db: AsyncSession, key: str, text: str) -> None:
    if key not in _PROMPT_BY_KEY:
        raise KeyError(f"unknown prompt key: {key}")
    await set_setting(db, OVERRIDE_PREFIX + key, text or "")


async def clear_override(db: AsyncSession, key: str) -> None:
    if key not in _PROMPT_BY_KEY:
        raise KeyError(f"unknown prompt key: {key}")
    await set_setting(db, OVERRIDE_PREFIX + key, "")
