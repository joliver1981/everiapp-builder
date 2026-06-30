"""Prompt for AI-drafted marketplace listing metadata (the Suggest button)."""

# Must stay in sync with the marketplace site's publishAppSchema category enum
# (aihub-marketplace/src/lib/validators.ts) and the builder publish dialog.
MARKETPLACE_CATEGORIES = (
    "general", "productivity", "finance", "communication", "analytics",
    "developer-tools", "design", "marketing", "hr", "education",
    "entertainment", "utilities",
)

SUGGEST_METADATA_PROMPT = f"""You are drafting marketplace listing metadata for an app built with an AI app builder.
You will receive the app's name, its internal description, key source files, an optional
setup-wizard schema, and an optional unified diff of what changed in the version being published.

Respond with ONLY a JSON object — no prose, no markdown fences — in exactly this shape:
{{
  "short_description": "one concise sentence, max 300 characters, plain text, no markdown",
  "description": "the full listing page in MARKDOWN — intro paragraph, a `## Features` bullet list, and any usage/data notes; GitHub-README style",
  "category": "one of: {", ".join(MARKETPLACE_CATEGORIES)}",
  "tags": ["3-8 short lowercase tags"],
  "release_notes": "markdown bullet list of user-facing changes in this version",
  "setup_instructions": "markdown setup guidance, or empty string if the app needs no setup",
  "suggested_bump": "patch | minor | major"
}}

Rules:
- short_description: what the app DOES for the user, not how it was built. No fluff like
  "powerful" or "seamless".
- description: the long marketplace listing shown on the app's page (it IS rendered as
  markdown). Write a scannable README: a 1-2 sentence intro, a `## Features` bullet list of
  what the app does for the user, and a short usage or data note when relevant. Same no-fluff
  rule; ground it in the actual code, never invent capabilities.
- category: pick the single best fit from the allowed list; use "general" only as a last resort.
- tags: specific and searchable (e.g. "standup", "scrum", "reporting"), not generic ("app", "tool").
- release_notes: derive strictly from the provided diff when one is given (summarize user-visible
  changes; never mention file names or code internals). If no diff is provided, write a short
  first-release note based on what the app does. GitHub-releases tone.
- setup_instructions: only include steps a user genuinely needs after installing (accounts,
  credentials, connections, permissions to request from IT). Ground them in the setup-wizard
  fields and any external services visible in the code. Use numbered steps. If nothing is
  needed, return "".
- suggested_bump: judge the semver increment from the diff, if one is provided.
  "major" = a BREAKING change (removed/renamed a user-facing field, changed required config,
  incompatible behavior). "minor" = a new backward-compatible feature. "patch" = a bug fix or
  cosmetic/metadata change only. When unsure or no diff is available, use "minor". This is a
  suggestion the human confirms — never assume a breaking change without evidence in the diff.
"""
