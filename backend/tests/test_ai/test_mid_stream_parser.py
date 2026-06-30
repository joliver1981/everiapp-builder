"""Pure tests for the smart prose/code stream splitter in src.ai.service.

This is the highest-value backend test for the live-code feature — the state machine
that decides what's prose (-> `text`) vs file content (-> `code_stream`) is subtle, runs
char-by-char across chunk boundaries, and must (a) keep prose byte-identical to the old
suppress-the-code behaviour and (b) agree with parse_llm_response about which files exist.
No DB/LLM.
"""
import pytest

from src.ai.service import _StreamState, _smart_stream_events, _flush_stream_events
from src.ai.code_parser import parse_llm_response


def _run(fragments):
    """Feed fragments through the splitter with persistent state; return event tuples."""
    st = _StreamState()
    events = []
    for frag in fragments:
        events.extend(_smart_stream_events(frag, st))
    events.extend(_flush_stream_events(st))
    return events


def _text(events):
    return "".join(p for k, p in events if k == "text")


def _code(events):
    return [p for k, p in events if k == "code_stream"]


# --- prose byte-identity (locks the refactor against the original algorithm) ---

def _original_text(full):
    """The pre-refactor suppress-code algorithm, inlined to lock byte-identity."""
    out, in_code, buf = [], False, ""
    for ch in full:
        buf += ch
        if not in_code:
            if buf.endswith("```"):
                in_code = True
                buf = ""
                out.append("\n\n")
            elif len(buf) > 3:
                out.append(buf[:-3])
                buf = buf[-3:]
        else:
            if buf.endswith("```"):
                in_code = False
                buf = ""
    if buf and not in_code:
        out.append(buf)
    return "".join(out)


@pytest.mark.parametrize("full", [
    "just prose, no code at all",
    "intro\n\n```tsx\n// FILE: src/App.tsx\nconst x = 1\n```\noutro",
    "run `npm i` then ```bash\nls\n``` then done",
    "trailing fence chars ``",
    "```tsx\n// FILE: a.tsx\nx\n```",
    "",
])
def test_prose_byte_identical_to_original(full):
    assert _text(_run([full])) == _original_text(full)


# --- code_stream behaviour ---

def test_single_file_block_streams_body_and_suppresses_from_prose():
    resp = (
        "Building it.\n\n"
        "```tsx\n// FILE: src/App.tsx\n"
        "export default function App() {\n"
        "  return <div/>\n"
        "}\n```\n"
        "Done."
    )
    events = _run([resp])
    code = _code(events)
    assert code[0] == {"event": "file_start", "path": "src/App.tsx"}
    assert code[-1] == {"event": "file_end", "path": "src/App.tsx"}
    body = "".join(e["text"] for e in code if e["event"] == "delta")
    assert body.rstrip("\n") == "export default function App() {\n  return <div/>\n}"
    txt = _text(events)
    assert "Building it." in txt and "Done." in txt
    assert "export default" not in txt           # code never leaks into the chat bubble


def test_panel_file_starts_match_parser():
    """Consistency: the files the panel shows == the files parse_llm_response saves."""
    resp = (
        "```tsx\n// FILE: src/A.tsx\nconst a = 1\n```\n\n"
        "```tsx\n// FILE: src/B.tsx\nconst b = 2\n```\n"
    )
    started = [e["path"] for e in _code(_run([resp])) if e["event"] == "file_start"]
    files, _, _ = parse_llm_response(resp)
    assert started == [f.path for f in files] == ["src/A.tsx", "src/B.tsx"]


def test_header_and_fence_split_across_chunks():
    """Real token streaming splits the fence and `// FILE:` header mid-token."""
    events = _run([
        "intro\n\n``",
        "`tsx\n// FIL",
        "E: src/App.tsx\nconst x =",
        " 1\n``",
        "`\noutro",
    ])
    code = _code(events)
    assert any(e["event"] == "file_start" and e["path"] == "src/App.tsx" for e in code)
    body = "".join(e["text"] for e in code if e["event"] == "delta")
    assert body.strip() == "const x = 1"
    assert "intro" in _text(events) and "outro" in _text(events)


def test_headerless_block_emits_no_code_stream():
    """A ```bash example in prose has no // FILE: header — parse ignores it and so does
    the panel, but the prose placeholder + surrounding text still flow."""
    resp = "Run this:\n\n```bash\nnpm install\nnpm run dev\n```\n\nThen open it."
    events = _run([resp])
    assert _code(events) == []
    files, _, _ = parse_llm_response(resp)
    assert files == []
    txt = _text(events)
    assert "Run this:" in txt and "Then open it." in txt
    assert "npm install" not in txt


def test_no_language_tag_block():
    resp = "```\n// FILE: src/x.ts\nexport const x = 1\n```\n"
    code = _code(_run([resp]))
    assert any(e["event"] == "file_start" and e["path"] == "src/x.ts" for e in code)
    body = "".join(e["text"] for e in code if e["event"] == "delta")
    assert body.strip() == "export const x = 1"


def test_second_file_header_in_body_is_content_not_a_new_file():
    """parse_llm_response honors only ONE // FILE: per block; the panel must agree."""
    resp = (
        "```tsx\n// FILE: src/App.tsx\n"
        "const a = 1\n"
        "// FILE: src/Other.tsx\n"     # a comment inside App.tsx, NOT a second file
        "const b = 2\n```\n"
    )
    events = _run([resp])
    starts = [e for e in _code(events) if e["event"] == "file_start"]
    assert len(starts) == 1 and starts[0]["path"] == "src/App.tsx"
    files, _, _ = parse_llm_response(resp)
    assert [f.path for f in files] == ["src/App.tsx"]
    body = "".join(e["text"] for e in _code(events) if e["event"] == "delta")
    assert "// FILE: src/Other.tsx" in body


def test_unterminated_block_closed_at_eof():
    """Truncated output: a block that never gets its closing fence. The flush must still
    emit file_end so a live panel doesn't hang on a perpetual 'writing…'."""
    events = _run(["```tsx\n// FILE: src/x.ts\nexport const x = 1\n"])
    code = _code(events)
    assert any(e["event"] == "file_start" for e in code)
    assert any(e["event"] == "file_end" for e in code)
    body = "".join(e["text"] for e in code if e["event"] == "delta")
    assert "export const x = 1" in body
