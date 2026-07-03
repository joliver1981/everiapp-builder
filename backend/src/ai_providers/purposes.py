"""Catalog of LLM-call purposes and their default-provider resolution.

A *purpose* names WHY the platform is calling an LLM (app generation, the
in-app assistant, the bug-report analyzer, ...). Admins can pin a provider —
and optionally a model — per purpose from Admin → AI Providers; anything
unpinned inherits the generation default, so a fresh install needs exactly
one configured provider to light everything up.

Resolution order (see AIProviderService._resolve_purpose):
  1. explicit pin      platform_settings "ai_provider_purpose_default.<purpose>"
  2. legacy boolean    provider metadata is_default_generation / is_default_toggle
  3. generation        steps 1–2 for "generation" (purposes other than generation)
  4. first active provider

Only purposes that are actually consulted for provider selection belong here —
usage-metering purposes are a wider set (e.g. "self_heal" rides the generation
turn's provider on purpose: a fix must be written by the same model that wrote
the code, so it is never pinned separately).
"""

# platform_settings key prefix; value is {"provider_id": str, "model": str | None}
# or null when the pin has been cleared.
PURPOSE_SETTING_PREFIX = "ai_provider_purpose_default."

PURPOSES: dict[str, dict] = {
    "generation": {
        "label": "App generation",
        "description": "Builder chat — generating and editing app code. "
                       "Self-heal fix calls deliberately reuse this provider.",
        "legacy_field": "is_default_generation",
    },
    "toggle": {
        "label": "In-app AI assistant",
        "description": "The AI Toggle chat inside running apps.",
        "legacy_field": "is_default_toggle",
    },
    "bug_analysis": {
        "label": "Bug-report analyzer",
        "description": "Analyzes submitted bug reports and drafts fix suggestions.",
        "legacy_field": None,
    },
    "marketplace_metadata": {
        "label": "Marketplace listing suggestions",
        "description": "Drafts listing metadata (description, category, tags) in the publish dialog.",
        "legacy_field": None,
    },
}
