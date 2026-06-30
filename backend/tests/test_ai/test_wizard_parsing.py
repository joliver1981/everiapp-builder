"""Wizard extraction from LLM responses.

The wizard-generation prompt instructs "respond with ONLY the JSON", but the
parser used to extract wizards only from the legacy {files, wizard} format —
so AI-generated wizards were silently dropped (and, for fenced replies, the
wizard's own description leaked into the chat as prose). These lock the fix.
"""
import json

from src.ai.code_parser import parse_llm_response

WIZARD = {
    "title": "App Setup",
    "description": "Configure the app",
    "steps": [
        {"title": "Step 1", "fields": [
            {"key": "api_key", "label": "API Key", "type": "secret", "required": True},
        ]},
    ],
}


def test_bare_wizard_json():
    """Fence-less reply that is exactly the wizard object."""
    files, desc, wizard = parse_llm_response(json.dumps(WIZARD))
    assert files == []
    assert wizard == WIZARD
    assert desc  # some friendly text, never empty


def test_bare_wizard_with_prose_around_it():
    content = "Sure! Here's your setup wizard:\n" + json.dumps(WIZARD) + "\nLet me know."
    files, desc, wizard = parse_llm_response(content)
    assert wizard == WIZARD
    assert "Sure!" in desc and "Let me know." in desc


def test_fenced_wizard_json():
    content = "Here you go:\n```json\n" + json.dumps(WIZARD, indent=2) + "\n```\nDone."
    files, desc, wizard = parse_llm_response(content)
    assert files == []
    assert wizard == WIZARD
    # The wizard's own description must NOT leak into the chat text.
    assert "Configure the app" not in desc
    assert "Here you go:" in desc


def test_wizard_with_braces_in_strings():
    """Brace-scan fallback must not choke on { } inside string values."""
    w = json.loads(json.dumps(WIZARD))
    w["steps"][0]["fields"][0]["placeholder"] = "e.g. {tenant}/{site}"
    files, desc, wizard = parse_llm_response(json.dumps(w))
    assert wizard == w


def test_legacy_format_still_extracts_wizard_and_files():
    legacy = {
        "files": [{"path": "src/App.tsx", "content": "export {}"}],
        "description": "made an app",
        "wizard": WIZARD,
    }
    content = "```json\n" + json.dumps(legacy) + "\n```"
    files, desc, wizard = parse_llm_response(content)
    assert len(files) == 1 and files[0].path == "src/App.tsx"
    assert wizard == WIZARD
    assert "made an app" in desc


def test_plain_json_that_is_not_a_wizard_stays_conversational():
    """A bare JSON object without steps must not be misread as a wizard."""
    content = json.dumps({"answer": 42, "reason": "math"})
    files, desc, wizard = parse_llm_response(content)
    assert wizard is None
    assert files == []


def test_file_blocks_take_priority():
    content = (
        "New component:\n```tsx\n// FILE: src/Thing.tsx\nexport const T = 1\n```"
    )
    files, desc, wizard = parse_llm_response(content)
    assert len(files) == 1 and files[0].path == "src/Thing.tsx"
    assert wizard is None
