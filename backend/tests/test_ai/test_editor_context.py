"""Pure tests for _format_editor_context — the in-code overlay's 'what the user is looking
at' system message that focuses the model on the file/selection on screen. No DB/LLM."""
from src.ai.service import _format_editor_context


def test_none_and_garbage_return_empty():
    assert _format_editor_context(None) == ""
    assert _format_editor_context({}) == ""
    assert _format_editor_context({"path": ""}) == ""
    assert _format_editor_context("not a dict") == ""


def test_file_only_no_selection():
    out = _format_editor_context({"path": "src/App.tsx", "viewportStartLine": 1, "viewportEndLine": 30})
    assert "src/App.tsx" in out
    assert "lines 1-30 on screen" in out
    assert "SELECTED" not in out
    assert "unless they say otherwise" in out


def test_file_without_viewport():
    out = _format_editor_context({"path": "src/App.tsx"})
    assert "viewing `src/App.tsx`" in out
    assert "on screen" not in out


def test_selection_range():
    out = _format_editor_context({
        "path": "src/hooks/useInventory.ts",
        "viewportStartLine": 10, "viewportEndLine": 60,
        "selectionText": "const PRODUCTS_KEY = 'x'", "selStartLine": 15, "selEndLine": 18,
    })
    assert "SELECTED this code (lines 15-18)" in out
    assert "const PRODUCTS_KEY" in out
    assert "```" in out


def test_single_line_selection_says_line_not_lines():
    out = _format_editor_context({"path": "a.ts", "selectionText": "x", "selStartLine": 7, "selEndLine": 7})
    assert "(line 7)" in out
    assert "(lines 7-7)" not in out


def test_selection_is_truncated():
    big = "z" * 5000
    out = _format_editor_context({"path": "a.ts", "selectionText": big, "selStartLine": 1, "selEndLine": 200})
    assert "selection truncated" in out
    assert out.count("z") == 4000          # capped at 4000 chars


def test_invalid_viewport_ignored():
    # Non-int or reversed ranges fall back to the no-range phrasing rather than crashing.
    out = _format_editor_context({"path": "a.ts", "viewportStartLine": 40, "viewportEndLine": 10})
    assert "viewing `a.ts`" in out
    assert "on screen" not in out
