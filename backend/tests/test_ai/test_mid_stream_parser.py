"""Pure tests for the smart prose/code stream splitter in src.ai.service.

This is the highest-value backend test for the live-code feature — the state machine
that decides what's prose (-> `text`) vs file content (-> `code_stream`) is subtle, runs
char-by-char across chunk boundaries, and must (a) keep prose byte-identical to the old
suppress-the-code behaviour and (b) agree with parse_llm_response about which files exist.
No DB/LLM.
"""
import pytest

from src.ai.service import (
    _StreamState,
    _smart_stream_events,
    _flush_stream_events,
    _coalesce_text_events,
)
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


# --- text-event coalescing (one WS message per LLM delta, not per character) ---

def _run_coalesced(fragments, live_code=True):
    """Feed fragments through the splitter + coalescer exactly as chat() does."""
    st = _StreamState()
    events = []
    for frag in fragments:
        evs = _smart_stream_events(frag, st)
        if not live_code:
            evs = (e for e in evs if e[0] != "code_stream")
        events.extend(_coalesce_text_events(evs))
    evs = _flush_stream_events(st)
    if not live_code:
        evs = (e for e in evs if e[0] != "code_stream")
    events.extend(_coalesce_text_events(evs))
    return events


def test_coalesce_one_text_event_per_prose_delta():
    """The splitter emits prose per-char; the wire must carry one event per delta."""
    frags = ["Hello there, ", "let me build that dashboard ", "for you now."]
    events = _run_coalesced(frags)
    texts = [p for k, p in events if k == "text"]
    # One event per fragment, +1 allowed for the flush of the 3-char lookbehind tail.
    assert len(texts) <= len(frags) + 1
    assert "".join(texts) == "Hello there, let me build that dashboard for you now."


def test_coalesce_output_byte_identical_to_uncoalesced():
    frags = [
        "intro\n\n``",
        "`tsx\n// FIL",
        "E: src/App.tsx\nconst x =",
        " 1\n``",
        "`\noutro",
    ]
    assert _text(_run_coalesced(frags)) == _text(_run(frags))


def test_coalesce_preserves_order_around_code_stream():
    """Prose before a file block must still arrive before file_start, and prose after
    the block after file_end — coalescing must not reorder across code_stream events."""
    events = _run_coalesced([
        "before\n\n```tsx\n// FILE: src/A.tsx\nconst a = 1\n```\nafter"
    ])
    kinds = []
    for k, p in events:
        if k == "text":
            kinds.append(("text", p))
        else:
            kinds.append(("code_stream", p["event"]))
    start = kinds.index(("code_stream", "file_start"))
    end = kinds.index(("code_stream", "file_end"))
    before = "".join(p for k, p in kinds[:start] if k == "text")
    after = "".join(p for k, p in kinds[end + 1:] if k == "text")
    assert "before" in before
    assert "after" in after and "after" not in before


def test_coalesce_with_live_off_merges_across_suppressed_block():
    """When code_stream is filtered out (live_code=False), the prose around a file block
    within one delta coalesces fully — the dropped events must not split it."""
    events = _run_coalesced(
        ["before\n\n```tsx\n// FILE: src/A.tsx\nconst a = 1\n```\nafter"],
        live_code=False,
    )
    assert all(k == "text" for k, _ in events)
    joined = "".join(p for _, p in events)
    assert "before" in joined and "after" in joined
    assert "const a = 1" not in joined            # code still suppressed from the bubble
