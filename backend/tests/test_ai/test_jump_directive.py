"""Pure tests for `extract_jump_directives` — the [[jump:...]] parser that powers
auto-jump + clickable code chips in the builder. No DB/LLM."""
from src.ai.code_parser import extract_jump_directives, parse_llm_response


def test_range_directive():
    refs, cleaned = extract_jump_directives("See [[jump:src/App.tsx:10-20]] for the totals.")
    assert refs == [{"path": "src/App.tsx", "start": 10, "end": 20}]
    assert "[[jump" not in cleaned
    assert cleaned == "See for the totals."


def test_single_line_directive():
    refs, _ = extract_jump_directives("Look at [[jump:src/hooks/useTotals.ts:42]].")
    assert refs == [{"path": "src/hooks/useTotals.ts", "start": 42, "end": 42}]


def test_whole_file_directive():
    refs, _ = extract_jump_directives("It's all in [[jump:src/App.tsx]] really.")
    assert refs == [{"path": "src/App.tsx", "start": None, "end": None}]


def test_multiple_directives_dedup_and_order():
    text = "[[jump:src/a.tsx:1-2]] and [[jump:src/b.tsx]] and [[jump:src/a.tsx:1-2]] again"
    refs, cleaned = extract_jump_directives(text)
    assert refs == [
        {"path": "src/a.tsx", "start": 1, "end": 2},
        {"path": "src/b.tsx", "start": None, "end": None},
    ]
    assert "jump" not in cleaned


def test_whitespace_tolerant():
    refs, _ = extract_jump_directives("x [[ jump : src/App.tsx : 5 - 9 ]] y")
    assert refs == [{"path": "src/App.tsx", "start": 5, "end": 9}]


def test_no_directives_returns_text_unchanged():
    refs, cleaned = extract_jump_directives("nothing to jump to here")
    assert refs == []
    assert cleaned == "nothing to jump to here"


def test_directive_in_code_body_stays_in_file_not_refs():
    """The contract: chat() runs extraction on the DESCRIPTION (prose), which
    parse_llm_response has already stripped of file bodies. So a [[jump:...]] that
    lands inside a file body is kept as file content and never surfaces as a ref."""
    resp = (
        "Here you go. [[jump:src/App.tsx:1-2]]\n\n"
        "```tsx\n// FILE: src/App.tsx\n"
        "// note: not a real directive [[jump:evil.ts:99]]\n"
        "export default function App(){ return null }\n```\n"
    )
    files, description, _ = parse_llm_response(resp)
    refs, cleaned = extract_jump_directives(description)
    assert refs == [{"path": "src/App.tsx", "start": 1, "end": 2}]
    assert "[[jump:evil.ts:99]]" in files[0].content      # left untouched in the file
    assert "evil.ts" not in str(refs)
