"""litellm model routing: ALWAYS provider-prefixed.

The bug this locks out (v0.17.3): OpenAI providers passed the BARE model name
to litellm and relied on its built-in model registry to infer the provider.
Any OpenAI model newer than the pinned litellm release (e.g. our own preset
defaults gpt-5.5 / gpt-5.4) failed with "LLM Provider NOT provided" — on a
fresh install, with the out-of-the-box defaults. Anthropic worked the whole
time because it was prefixed. litellm_model() composes the route explicitly
so provider inference never runs.
"""
from __future__ import annotations

import re
from pathlib import Path

from src.llm_compat import litellm_model

_SRC = Path(__file__).resolve().parents[1] / "src"


def test_openai_bare_model_gets_prefixed():
    # The exact ids that failed in the field, plus a future-proof stranger.
    assert litellm_model("openai", "gpt-5.5") == "openai/gpt-5.5"
    assert litellm_model("openai", "gpt-5.4") == "openai/gpt-5.4"
    assert litellm_model("openai", "gpt-99-ultra") == "openai/gpt-99-ultra"


def test_other_providers_prefixed_unchanged_behavior():
    assert litellm_model("anthropic", "claude-opus-4-8") == "anthropic/claude-opus-4-8"
    assert litellm_model("ollama", "llama3") == "ollama/llama3"


def test_admin_typed_prefix_not_doubled():
    assert litellm_model("openai", "openai/gpt-5.4") == "openai/gpt-5.4"
    assert litellm_model("anthropic", "anthropic/claude-opus-4-8") == "anthropic/claude-opus-4-8"


def test_provider_native_ids_with_slashes_still_get_routed():
    # OpenRouter model ids natively contain "/" — they must still be routed
    # through openrouter, not mistaken for an explicit litellm route.
    assert (
        litellm_model("openrouter", "anthropic/claude-3-opus")
        == "openrouter/anthropic/claude-3-opus"
    )


def test_edges_whitespace_and_missing_parts():
    assert litellm_model("openai", "  gpt-5.5  ") == "openai/gpt-5.5"
    assert litellm_model("", "gpt-5.5") == "gpt-5.5"
    assert litellm_model(None, "gpt-5.5") == "gpt-5.5"
    assert litellm_model("openai", "") == ""
    assert litellm_model("openai", None) == ""


def test_no_call_site_special_cases_openai_again():
    """Source contract: the old `model if provider_type == "openai" else ...`
    idiom must never come back — one copy-pasted revival reintroduces the
    fresh-install failure. All composition goes through litellm_model()."""
    pattern = re.compile(r'[=!]=\s*["\']openai["\']')
    offenders = []
    for py in _SRC.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{py.relative_to(_SRC)}:{i}: {line.strip()}")
    assert not offenders, (
        "provider_type == 'openai' special-casing found — route models through "
        "llm_compat.litellm_model() instead:\n" + "\n".join(offenders)
    )
