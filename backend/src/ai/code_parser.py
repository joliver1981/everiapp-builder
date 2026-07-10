import json
import re
from .schemas import GeneratedFile


# --- Jump directives ------------------------------------------------------
# The model emits [[jump:path]] / [[jump:path:LINE]] / [[jump:path:START-END]] in
# its PROSE (never inside a code block) when the user asks to see/locate specific
# code. The builder uses these to auto-navigate the Code panel and highlight the
# lines. We parse them out of the DESCRIPTION (not the raw response) so a stray
# directive that lands inside a file body can never corrupt the saved file.
_JUMP_RE = re.compile(
    r'\[\[\s*jump\s*:\s*'        # [[jump:
    r'([^\]:]+?)'                 # group 1: path (no ':' or ']')
    r'\s*'
    r'(?::\s*(\d+)\s*(?:-\s*(\d+))?\s*)?'  # optional :START or :START-END (groups 2,3)
    r'\]\]',                      # ]]
    re.IGNORECASE,
)


def extract_jump_directives(text: str) -> tuple[list[dict], str]:
    """Pull ``[[jump:...]]`` directives out of ``text``.

    Returns ``(refs, cleaned_text)`` where each ref is
    ``{"path": str, "start": int | None, "end": int | None}`` in order of first
    appearance (deduplicated on the whole tuple). ``start``/``end`` are ``None``
    when the directive names only a file; a single line (``[[jump:path:42]]``)
    yields ``end == start``. ``cleaned_text`` has the directives removed and
    leftover whitespace tidied.
    """
    if not text or "[[" not in text:
        return [], text

    refs: list[dict] = []
    seen: set[tuple] = set()
    for m in _JUMP_RE.finditer(text):
        path = (m.group(1) or "").strip()
        if not path:
            continue
        start = int(m.group(2)) if m.group(2) else None
        end = int(m.group(3)) if m.group(3) else start
        key = (path, start, end)
        if key in seen:
            continue
        seen.add(key)
        refs.append({"path": path, "start": start, "end": end})

    if not refs:
        return [], text

    # Replace directives with a sentinel first: when the model wrapped one in
    # emphasis or inline code (**[[jump:…]]**, `[[jump:…]]`), plain removal
    # leaves an empty husk ("****", "``") that markdown renders as literal
    # punctuation. Unwrapping is anchored to the sentinel so real formatting
    # elsewhere in the prose is never touched.
    cleaned = _JUMP_RE.sub("\x00", text)
    cleaned = re.sub(r'\x00(?:\s*\x00)+', '\x00', cleaned)   # adjacent directives
    for _ in range(2):  # twice: handles nesting like **`[[jump:…]]`**
        cleaned = re.sub(r'`\s*\x00\s*`', '\x00', cleaned)
        cleaned = re.sub(r'\*{1,2}\s*\x00\s*\*{1,2}', '\x00', cleaned)
        cleaned = re.sub(r'_\s*\x00\s*_', '\x00', cleaned)
    cleaned = cleaned.replace('\x00', '')
    # Tidy whitespace the removal left behind.
    cleaned = re.sub(r'[ \t]+\n', '\n', cleaned)     # trailing spaces on a line
    cleaned = re.sub(r'[ \t]{2,}', ' ', cleaned)     # double spaces mid-line
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)     # 3+ blank lines
    return refs, cleaned.strip()


def parse_llm_response(content: str) -> tuple[list[GeneratedFile], str, dict | None]:
    """Parse LLM response to extract generated files, description, and optional wizard schema.

    Supports two formats:
    1. Individual code blocks with // FILE: headers (preferred)
    2. JSON code block with files array (legacy)

    Returns (files, description, wizard_schema).
    """
    files: list[GeneratedFile] = []
    description_parts: list[str] = []
    wizard = None

    # --- Format 1: Individual code blocks with FILE headers ---
    # Pattern: ```lang\n// FILE: path\ncontent\n```
    # `# FILE:` is the same header in Python comment syntax — models writing
    # server functions (server/functions/*.py) naturally use it.
    file_block_pattern = re.compile(
        r'```(\w*)\s*\n'                  # opening fence with optional language
        r'(?://|#)\s*FILE:\s*(\S+)\s*\n'  # FILE header with path
        r'(.*?)'                          # file content
        r'\n```',                         # closing fence
        re.DOTALL,
    )

    file_matches = list(file_block_pattern.finditer(content))

    if file_matches:
        # Extract files from code blocks
        for m in file_matches:
            path = m.group(2).strip()
            file_content = m.group(3)
            if path and file_content:
                files.append(GeneratedFile(path=path, content=file_content, action="create"))

        # Description is everything outside the file code blocks
        last_end = 0
        for m in file_matches:
            text_before = content[last_end:m.start()].strip()
            if text_before:
                description_parts.append(text_before)
            last_end = m.end()
        text_after = content[last_end:].strip()
        if text_after:
            description_parts.append(text_after)

        description = "\n\n".join(description_parts)
        return files, description, wizard

    # --- Format 2: JSON code block (legacy) ---
    json_match = re.search(r'```json\s*\n(.*?)\n```', content, re.DOTALL)
    if not json_match:
        json_match = re.search(r'```\s*\n(\{.*?\})\n```', content, re.DOTALL)

    if json_match:
        try:
            data = json.loads(json_match.group(1))

            # Text outside the JSON block
            text_before = content[:json_match.start()].strip()
            text_after = content[json_match.end():].strip()

            # The wizard-generation prompt instructs "respond with ONLY the JSON",
            # so a steps-shaped object (no files) IS the wizard schema — its own
            # "description" belongs to the wizard, not the chat transcript.
            if _wizard_shaped(data):
                description = "\n\n".join(p for p in (text_before, text_after) if p)
                return [], description or "I've generated a setup wizard for the app.", data

            for f in data.get("files", []):
                path = f.get("path", "")
                file_content = f.get("content", "")
                action = f.get("action", "create")
                if path and file_content:
                    files.append(GeneratedFile(path=path, content=file_content, action=action))

            description = data.get("description", "")
            if text_before:
                description = f"{text_before}\n\n{description}" if description else text_before
            if text_after:
                description = f"{description}\n\n{text_after}" if description else text_after

            wizard = data.get("wizard")
            return files, description.strip(), wizard
        except json.JSONDecodeError:
            pass

    # --- Format 3: bare JSON wizard (fence-less "ONLY the JSON" replies) ---
    wizard, leftover = _extract_bare_wizard(content)
    if wizard is not None:
        return [], leftover or "I've generated a setup wizard for the app.", wizard

    # --- No recognized format: treat entire response as conversational ---
    return [], content.strip(), None


def _wizard_shaped(data) -> bool:
    """A parsed object that IS a wizard schema (steps list, not a files payload).

    Steps must be non-empty objects whose `fields` (when present) are lists of
    objects. Any dict with a `steps` list used to qualify, so a reply like
    {"steps": ["clone the repo", "run npm install"]} — deployment steps as
    JSON — was saved over the app's setup wizard and then 500'd the setup
    endpoints. Shape-junk now stays conversational; schema-level validity
    (key format, types, duplicates) is the save paths' job via validate_wizard.
    """
    if not (isinstance(data, dict) and isinstance(data.get("steps"), list) and not data.get("files")):
        return False
    steps = data["steps"]
    if not steps:
        return False
    for step in steps:
        if not isinstance(step, dict):
            return False
        fields = step.get("fields", [])
        if not isinstance(fields, list) or not all(isinstance(f, dict) for f in fields):
            return False
    return True


def _extract_bare_wizard(content: str) -> tuple[dict | None, str]:
    """Find a fence-less wizard JSON object in the response.

    Returns (wizard, text_outside) — (None, "") when there isn't one. Tries the
    widest brace span first (handles braces inside string values), then a
    depth-scan for objects embedded in prose.
    """
    start = content.find("{")
    if start == -1:
        return None, ""

    end_wide = content.rfind("}")
    candidates = []
    if end_wide > start:
        candidates.append(end_wide)

    depth = 0
    for i in range(start, len(content)):
        if content[i] == "{":
            depth += 1
        elif content[i] == "}":
            depth -= 1
            if depth == 0:
                if i != end_wide:
                    candidates.append(i)
                break

    for end in candidates:
        try:
            data = json.loads(content[start:end + 1])
        except json.JSONDecodeError:
            continue
        if _wizard_shaped(data):
            outside = (content[:start] + " " + content[end + 1:]).strip()
            return data, outside
        return None, ""  # valid JSON but not a wizard — leave it to other handling
    return None, ""
