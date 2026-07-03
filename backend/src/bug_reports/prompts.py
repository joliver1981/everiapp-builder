"""Prompts used by the bug-report analyzer.

The LLM is asked to produce a strict JSON response. We avoid the streaming
free-form file-block format used in `ai/prompts.py` here because we need
deterministic, structured output for the admin UI's diff view + risk gating.
"""

ANALYZER_SYSTEM_PROMPT = """You are a senior engineer triaging a bug report for a small React + TypeScript + Vite single-page app.

You will receive:
1. The bug report (title, description, captured context like console errors, network failures, page URL)
2. The relevant source files of the app at the version that was running when the bug was reported

Your job is to:
1. Diagnose what's going wrong, in plain English
2. Identify the most likely root cause, citing specific files/lines if possible
3. Propose a minimal, surgical fix as a set of file changes
4. Assess the risk of applying the fix automatically

RISK LEVELS — be honest, default to higher risk when unsure:
- "low": copy/text/CSS-only edits, single-line changes, type annotation fixes, removing dead code, log/comment changes. Cannot break anything important.
- "medium": logic changes confined to one component or function. Touches state but not data flow. New conditional. Reordered effects.
- "high": structural changes (multiple files), changes to data fetching/auth/routing, dependency additions, anything that touches secrets or storage, changes to the build config, changes that delete files, changes that you are uncertain will fix the bug.

OUTPUT FORMAT — respond with ONE fenced ```json block, no prose before or after:
```json
{
  "diagnosis": "Brief human-readable explanation of what's broken.",
  "root_cause": "The most likely root cause, with file/line references when applicable.",
  "proposed_files": [
    {
      "path": "src/components/Foo.tsx",
      "action": "update",
      "content": "// full file content after the fix\\n..."
    }
  ],
  "risk_level": "low",
  "risk_rationale": "Why this risk level was chosen — be specific about what could go wrong if this fix is wrong."
}
```

RULES:
- `proposed_files[].action` is one of "create", "update", "delete"
- For "update" and "create", include the COMPLETE file content (not a diff). The platform overwrites the file.
- For "delete", `content` may be empty.
- Keep changes MINIMAL. Do not refactor. Do not "improve" unrelated code.
- Only include files you are actually changing.
- If you cannot identify a fix with reasonable confidence, set risk_level to "high" and explain in risk_rationale; still propose your best guess if you have one, otherwise return an empty proposed_files array.
- Never include secrets, API keys, or hardcoded credentials in the output.
- The output MUST be valid JSON. Escape special characters in `content` properly.
"""


def build_analyzer_user_prompt(
    *,
    bug_title: str,
    bug_description: str,
    captured_context: dict,
    files: list[dict],  # [{path, content}]
    version: int | None,
    extra_note: str = "",
) -> str:
    """Construct the user-turn prompt fed to the analyzer."""
    parts: list[str] = []

    parts.append("# Bug report")
    parts.append(f"**Title:** {bug_title}")
    if bug_description:
        parts.append(f"**Description:**\n{bug_description}")
    if version is not None:
        parts.append(f"**App version when reported:** v{version}")

    if captured_context:
        parts.append("\n# Captured context")
        page_url = captured_context.get("page_url")
        if page_url:
            parts.append(f"- Page URL: `{page_url}`")
        ua = captured_context.get("user_agent")
        if ua:
            parts.append(f"- User agent: `{ua}`")
        viewport = captured_context.get("viewport")
        if viewport:
            parts.append(f"- Viewport: {viewport}")
        console_tail = captured_context.get("console_tail") or []
        if console_tail:
            parts.append("- Recent console output:")
            for line in console_tail[-30:]:
                parts.append(f"    {line}")
        net_errors = captured_context.get("network_errors") or []
        if net_errors:
            parts.append("- Recent failed network requests:")
            for err in net_errors[-10:]:
                parts.append(
                    f"    {err.get('method', 'GET')} {err.get('url')} "
                    f"-> {err.get('status', 'ERR')} {err.get('error', '')}"
                )
        # Trace spine: the SDK attaches the session's recent client spans —
        # clicks, dataset/app-DB calls, UI errors, with timings. This is the
        # chronological "what happened", far more diagnostic than console text.
        spans = captured_context.get("recent_spans") or []
        if spans:
            trace_id = captured_context.get("trace_id")
            parts.append(f"- Traced events leading up to the report"
                         f"{f' (trace {trace_id})' if trace_id else ''}, oldest first:")
            for s in spans[-40:]:
                if not isinstance(s, dict):
                    continue
                line = (f"    [{s.get('kind', '?')}] {s.get('name', '')}"
                        f" — {s.get('status', '?')}")
                if s.get("latency_ms"):
                    line += f" in {s['latency_ms']}ms"
                if s.get("error"):
                    line += f" | error: {str(s['error'])[:300]}"
                if s.get("detail"):
                    line += f" | detail: {str(s['detail'])[:200]}"
                parts.append(line)

    if extra_note:
        parts.append(f"\n# Additional context from the human reviewer\n{extra_note}")

    parts.append("\n# Source files")
    for f in files:
        parts.append(f"\n## `{f['path']}`")
        parts.append(f"```\n{f['content']}\n```")

    parts.append("\nNow analyze and respond with the JSON described in the system prompt.")
    return "\n".join(parts)
