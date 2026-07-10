"""Preset registry for kind="ai" Connections — the well-known AI providers.

Single source of truth for what "add an OpenAI / Anthropic / OpenRouter /
Azure OpenAI connection" means: the default base URL, how the API key is
injected, extra headers/query params the provider requires, where its models
list lives, and the chat endpoint generated apps should call. The admin UI
reads this over GET /api/admin/connections/ai-providers to prefill the create
form; test-connection and fetch-models use it server-side; and the generation
prompt uses the chat hints so apps speak each provider's wire format.

Config contract for kind="ai" rows — the SAME keys the REST driver already
reads (base_url / auth_type / auth_param / default_headers / default_query),
so every existing build_client call site works unchanged, PLUS:
  provider:       key into AI_PROVIDERS ("custom" for anything else)
  models:         list of model-id strings the admin exposes to apps
  default_model:  the model apps use when the user doesn't pick one
  chat_path:      relative path of the chat endpoint (preset default, editable)
  models_path:    relative path of the provider's list-models endpoint

`api_format` tells the SDK's aiChat() how to shape the request body:
"anthropic" (Messages API) or "openai" (Chat Completions — OpenAI, OpenRouter,
Azure OpenAI, and virtually every self-hosted gateway are OpenAI-compatible).
"""
from __future__ import annotations

from .drivers.rest import AUTH_TYPES

# NOTE: `suggested_models` are starting points shown in the admin UI, not a
# catalog — "Fetch models" pulls the live list from the provider's API.
AI_PROVIDERS: dict[str, dict] = {
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "auth_type": "bearer",
        "auth_param": None,
        "default_headers": {},
        "default_query": {},
        "models_path": "/models",
        "chat_path": "/chat/completions",
        "api_format": "openai",
        "suggested_models": ["gpt-5", "gpt-5-mini", "gpt-5-nano", "gpt-4.1", "gpt-4o"],
        "hint": "Create an API key at platform.openai.com → API keys.",
    },
    "anthropic": {
        "label": "Anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "auth_type": "api_key_header",
        "auth_param": "x-api-key",
        # The Messages API rejects requests without an anthropic-version header.
        "default_headers": {"anthropic-version": "2023-06-01"},
        "default_query": {},
        "models_path": "/models",
        # /v1/models is paginated with a default page size of 20 — without this
        # a fetch silently truncates to the first page.
        "models_query": {"limit": "1000"},
        "chat_path": "/messages",
        "api_format": "anthropic",
        "suggested_models": [
            "claude-fable-5", "claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5",
        ],
        "hint": "Create an API key at console.anthropic.com → API keys.",
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "auth_type": "bearer",
        "auth_param": None,
        "default_headers": {},
        "default_query": {},
        "models_path": "/models",
        "chat_path": "/chat/completions",
        "api_format": "openai",
        "suggested_models": [
            "anthropic/claude-sonnet-5", "openai/gpt-5", "google/gemini-2.5-pro",
        ],
        "hint": "One key, hundreds of models — create it at openrouter.ai → Keys.",
    },
    "azure_openai": {
        "label": "Azure OpenAI",
        # The v1 API surface; replace YOUR-RESOURCE with the Azure resource name.
        "base_url": "https://YOUR-RESOURCE.openai.azure.com/openai/v1",
        "auth_type": "api_key_header",
        "auth_param": "api-key",
        "default_headers": {},
        "default_query": {"api-version": "preview"},
        "models_path": "/models",
        "chat_path": "/chat/completions",
        "api_format": "openai",
        "suggested_models": [],
        "hint": (
            "Replace YOUR-RESOURCE in the base URL with your Azure OpenAI resource "
            "name. Model ids are your DEPLOYMENT names (Azure portal → Deployments)."
        ),
    },
    "custom": {
        "label": "Custom (OpenAI-compatible)",
        "base_url": "",
        "auth_type": "bearer",
        "auth_param": None,
        "default_headers": {},
        "default_query": {},
        "models_path": "/models",
        "chat_path": "/chat/completions",
        "api_format": "openai",
        "suggested_models": [],
        "hint": "Any OpenAI-compatible endpoint (vLLM, Ollama, LM Studio, a gateway…).",
    },
}


def preset_for(config: dict) -> dict:
    """The AI_PROVIDERS entry for a config's provider, falling back to custom."""
    return AI_PROVIDERS.get((config or {}).get("provider") or "", AI_PROVIDERS["custom"])


def ai_chat_path(config: dict) -> str:
    """The chat endpoint path for an ai-kind config (explicit value or preset)."""
    return (config or {}).get("chat_path") or preset_for(config)["chat_path"]


def ai_models_path(config: dict) -> str:
    """The list-models endpoint path for an ai-kind config."""
    return (config or {}).get("models_path") or preset_for(config)["models_path"]


def ai_models_query(config: dict) -> dict:
    """Query params for the list-models call (e.g. Anthropic's pagination limit)."""
    return dict(preset_for(config).get("models_query") or {})


def ai_api_format(config: dict) -> str:
    """The request-body dialect ("openai" | "anthropic") for an ai-kind config."""
    fmt = (config or {}).get("api_format") or preset_for(config)["api_format"]
    return fmt if fmt in ("openai", "anthropic") else "openai"


def ai_models(config: dict) -> list[str]:
    """The admin-curated model ids, cleaned to non-empty strings."""
    raw = (config or {}).get("models") or []
    return [m.strip() for m in raw if isinstance(m, str) and m.strip()]


def validate_ai_config(config: dict) -> None:
    """Eager validation for kind="ai" configs at create/update time.

    Unlike sql/rest (validated lazily at call time), AI connections are meant
    to be turnkey — surface a misconfiguration to the admin immediately.
    Raises ValueError with a human-readable message (routers map it to 400).
    """
    cfg = config or {}
    provider = cfg.get("provider")
    if provider not in AI_PROVIDERS:
        raise ValueError(
            f"config.provider must be one of {sorted(AI_PROVIDERS)} for AI connections"
        )
    base_url = (cfg.get("base_url") or "").strip()
    if not base_url:
        raise ValueError("config.base_url is required for AI connections")
    if "YOUR-RESOURCE" in base_url:
        raise ValueError(
            "Replace YOUR-RESOURCE in the base URL with your Azure OpenAI resource name"
        )
    auth_type = cfg.get("auth_type", "none")
    if auth_type not in AUTH_TYPES:
        raise ValueError(f"Unknown auth_type '{auth_type}'. Known: {sorted(AUTH_TYPES)}")
    models = cfg.get("models")
    if models is not None:
        if not isinstance(models, list) or any(not isinstance(m, str) for m in models):
            raise ValueError("config.models must be a list of model-id strings")
    default_model = cfg.get("default_model")
    if default_model is not None and not isinstance(default_model, str):
        raise ValueError("config.default_model must be a string")
    for key in ("chat_path", "models_path"):
        val = cfg.get(key)
        if val is None:
            continue
        if not isinstance(val, str) or "://" in val or val.startswith("//"):
            raise ValueError(f"config.{key} must be a path relative to the base URL")


def parse_models_response(body) -> list[str]:
    """Extract model ids from a provider's list-models response.

    Every supported provider returns {"data": [{"id": ...}]} (OpenAI, Azure,
    OpenRouter, and Anthropic all follow it); accept a couple of common
    variants for OpenAI-compatible gateways. Returns sorted unique ids.
    """
    items = None
    if isinstance(body, dict):
        for key in ("data", "models"):
            if isinstance(body.get(key), list):
                items = body[key]
                break
    elif isinstance(body, list):
        items = body
    if items is None:
        return []
    ids: set[str] = set()
    for item in items:
        if isinstance(item, str) and item.strip():
            ids.add(item.strip())
        elif isinstance(item, dict):
            mid = item.get("id") or item.get("name") or item.get("model")
            if isinstance(mid, str) and mid.strip():
                ids.add(mid.strip())
    return sorted(ids)
