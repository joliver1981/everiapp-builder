"""Analyzer JSON parser: tolerate noise around the JSON, sane defaults, path safety."""
from src.bug_reports.analyzer import parse_analyzer_response


def test_parses_clean_json_block():
    raw = """Here's my analysis:
```json
{
  "diagnosis": "X",
  "root_cause": "Y",
  "proposed_files": [
    {"path": "src/App.tsx", "action": "update", "content": "// new"}
  ],
  "risk_level": "low",
  "risk_rationale": "Single-line copy fix"
}
```
Hope this helps.
"""
    r = parse_analyzer_response(raw)
    assert r.error is None
    assert r.diagnosis == "X"
    assert r.root_cause == "Y"
    assert r.risk_level == "low"
    assert len(r.proposed_files) == 1
    assert r.proposed_files[0]["path"] == "src/App.tsx"


def test_unknown_risk_level_becomes_high():
    """Anything we don't recognize is treated as 'high' so it can't auto-deploy."""
    raw = """```json
{"diagnosis":"x","root_cause":"y","proposed_files":[],"risk_level":"trivial","risk_rationale":"r"}
```"""
    r = parse_analyzer_response(raw)
    assert r.risk_level == "high"


def test_path_traversal_rejected():
    raw = """```json
{
  "diagnosis": "x", "root_cause": "y", "risk_level": "low", "risk_rationale": "r",
  "proposed_files": [
    {"path": "../../etc/passwd", "action": "update", "content": "x"},
    {"path": "/abs/path", "action": "update", "content": "x"},
    {"path": "src/legit.tsx", "action": "update", "content": "x"}
  ]
}
```"""
    r = parse_analyzer_response(raw)
    paths = [f["path"] for f in r.proposed_files]
    assert paths == ["src/legit.tsx"]


def test_invalid_action_normalized_to_update():
    raw = """```json
{
  "diagnosis":"x","root_cause":"y","risk_level":"low","risk_rationale":"r",
  "proposed_files":[{"path":"a.ts","action":"refactor","content":"x"}]
}
```"""
    r = parse_analyzer_response(raw)
    assert r.proposed_files[0]["action"] == "update"


def test_no_json_block_returns_error():
    r = parse_analyzer_response("Sorry, I can't analyze this.")
    assert r.error is not None
    assert r.proposed_files == []


def test_invalid_json_returns_error():
    raw = "```json\n{not really json}\n```"
    r = parse_analyzer_response(raw)
    assert r.error is not None
    assert "valid JSON" in r.error or "JSON" in r.error


def test_brace_balanced_extraction_fallback():
    """If there's no fence but a JSON object is present, grab the first balanced one."""
    raw = 'Some prose. {"diagnosis":"a","root_cause":"b","proposed_files":[],"risk_level":"medium","risk_rationale":"r"} Trailing prose.'
    r = parse_analyzer_response(raw)
    assert r.error is None
    assert r.diagnosis == "a"
    assert r.risk_level == "medium"
