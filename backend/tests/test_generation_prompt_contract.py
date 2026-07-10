"""The generation system prompt is the AI builder's ONLY knowledge of how the
platform works (generated apps never see SDK source). These assertions lock in
the architecture-awareness contract so a future edit can't silently drop it and
regress the builder into faking unsupported capabilities (the model-comparison
app that had one model role-play several was exactly this failure).
"""
from __future__ import annotations

from src.ai.prompts import SYSTEM_PROMPT
from src.ai.wizard_prompts import WIZARD_GENERATION_PROMPT


def test_prompt_states_the_frontend_on_platform_architecture():
    p = SYSTEM_PROMPT.lower()
    assert "do not have their own backend" in p or "not have their own backend server" in p
    assert "the everiapp platform is the shared backend" in p or "platform is the shared backend" in p


def test_prompt_uses_the_current_platform_brand_name():
    """The platform is branded EveriApp — the builder must call it that in chat,
    not the retired 'AIHub' name (the user saw the AI say "how this works on AIHub").
    The `@aihub/app-sdk` package and `window.__AIHUB_*` globals are code identifiers
    and legitimately keep the old token, so we assert on the human-facing NAME, not
    the mere absence of the substring 'aihub'."""
    p = SYSTEM_PROMPT.lower()
    assert "everiapp" in p
    assert "building apps for the everiapp platform" in p
    assert "the aihub platform" not in p          # never brand the platform "AIHub"
    # The setup-wizard generator (a separate AI feature) also names the platform.
    w = WIZARD_GENERATION_PROMPT.lower()
    assert "everiapp" in w
    assert "aihub" not in w                        # wizard prompt has no code identifiers


def test_prompt_names_the_only_server_side_paths():
    for hook in ("useAppQuery", "useDataset", "aiDecide"):
        assert hook in SYSTEM_PROMPT
    assert "Connection" in SYSTEM_PROMPT and "Dataset" in SYSTEM_PROMPT
    assert "AI Providers" in SYSTEM_PROMPT


def test_prompt_states_hard_limits_and_forbids_faking():
    p = SYSTEM_PROMPT.lower()
    # Hard limits present.
    assert "cannot" in p and "api key" in p
    assert "custom server-side code" in p
    # The anti-faking rule + the concrete role-play example that burned a user.
    assert "never" in p and ("fake" in p or "simulate" in p)
    assert "role-play" in p


def test_prompt_teaches_the_real_external_call_primitive():
    """External / multi-provider calls are now REAL via callConnection through an
    admin-configured, app-callable Connection — the AI must reach for that, not fake it."""
    p = SYSTEM_PROMPT.lower()
    assert "callconnection" in p
    assert "app-callable" in p and "connection" in p


def test_prompt_tells_the_ai_to_guide_users_to_configure_the_platform():
    p = SYSTEM_PROMPT.lower()
    assert "admin" in p and "configure" in p
    assert "guide" in p or "explain" in p


def test_prompt_makes_apps_zero_config_by_default():
    """Apps must reference ATTACHED resources by their given ids directly, never
    make users re-enter platform config (connection/dataset ids, keys, endpoints)."""
    p = SYSTEM_PROMPT.lower()
    assert "zero-config" in p or "zero config" in p
    assert "re-enter" in p or "re-declaring" in p or "already knows" in p
    assert "directly" in p


def test_connections_block_teaches_path_and_status_footguns():
    """The two live failures from the Model Compare app: (1) base URL ending in
    /v1 + path starting with /v1 → upstream 404 on /v1/v1/...; (2) callConnection
    RESOLVES with upstream errors (it doesn't throw), so apps must check
    res.status. The Available Connections block must teach both."""
    from src.ai.prompts import available_connections_block
    block = available_connections_block([
        {"id": "c1", "name": "openai-conn", "description": "", "base_url": "https://api.openai.com/v1"},
    ])
    b = block.lower()
    assert "never repeat a segment" in b or "do not repeat" in b or "don't repeat" in b
    assert "res.status" in b
    assert "resolves" in b  # upstream errors come back as a resolved response


def test_prompt_says_multiple_schema_declarations_are_safe():
    """useAppSchema declarations from several hooks/components each apply
    independently (the version-1 collision is fixed) — the prompt must not
    scare the AI away from the natural per-hook pattern."""
    p = SYSTEM_PROMPT.lower()
    assert "several hooks/components" in p or "multiple useappschema" in p
    assert "applied independently" in p


def test_prompt_teaches_first_class_ai_provider_connections():
    """AI providers are a first-class Connection kind: the prompt must teach
    aiChat (one request shape per provider), the admin-curated model list +
    default model, and name the preset providers — otherwise the builder keeps
    telling users to hand-configure generic REST connections."""
    p = SYSTEM_PROMPT.lower()
    assert "aichat" in p
    assert "ai provider connection" in p
    assert "default model" in p or "default_model" in p
    for provider in ("openai", "anthropic", "openrouter", "azure"):
        assert provider in p, f"prompt no longer names {provider}"


def test_connections_block_renders_ai_provider_models_and_aichat():
    """Bound AI connections must surface their models + default model to the
    builder, and teach aiChat with the resolve-don't-throw semantics
    callConnection established."""
    from src.ai.prompts import available_connections_block
    block = available_connections_block([
        {"id": "c1", "name": "rest-conn", "description": "", "base_url": "https://api.example.com"},
        {"id": "c2", "name": "anthropic-conn", "description": "Claude", "kind": "ai",
         "provider": "anthropic", "base_url": "https://api.anthropic.com/v1",
         "models": ["claude-sonnet-5", "claude-haiku-4-5"],
         "default_model": "claude-sonnet-5"},
    ])
    b = block.lower()
    assert "aichat" in b
    assert "claude-sonnet-5" in b and "claude-haiku-4-5" in b
    assert "default model" in b
    assert "ai provider: anthropic" in b
    assert "res.status" in b and "resolves" in b


def test_connections_block_without_ai_kind_omits_aichat_section():
    """A rest-only app must not be taught aiChat — it would call it against a
    connection the SDK will reject."""
    from src.ai.prompts import available_connections_block
    block = available_connections_block([
        {"id": "c1", "name": "rest-conn", "description": "", "base_url": "https://api.example.com"},
    ])
    assert "aiChat" not in block


def test_prompt_teaches_runtime_connection_discovery():
    """Multi-connection UIs must enumerate attached connections at RUNTIME via
    useConnections(), not bake a registry file into the source that has to be
    hand-edited per provider (the Model Compare app told its users to go edit
    src/hooks/useProviders.ts — exactly the failure this guards against).
    And in-app copy must never point users at source files or builder internals."""
    p = SYSTEM_PROMPT.lower()
    assert "useconnections" in p
    assert "runtime" in p
    # The anti-pattern is named: no hand-maintained registry of connections.
    assert "registry" in p and ("do not" in p or "never" in p)
    # User-facing copy rule: apps must not tell users to edit code.
    assert "never mention source files" in p
