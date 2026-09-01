"""Builder-chat attachments: screenshots, images, PDFs and text files a user
drops into the chat so a multimodal model can see them.

Pure helpers live here (unit-tested without a DB):

- :func:`classify_upload` decides whether a file is accepted and how it is
  sent to the model — an ``image`` becomes an OpenAI-format ``image_url``
  part, a ``pdf`` a ``file`` part (litellm translates both for Anthropic,
  OpenAI, Azure, OpenRouter, Gemini…), and a ``text`` file is inlined into
  the message text in a fenced block so even text-only models can read it.
- :func:`build_user_content` turns a message + its attachments into the
  ``content`` the LLM call receives.

Policy (owner directive 2026-08-30): the platform imposes NO size cap of its
own — only the provider's limits bind, and a rejection surfaces verbatim in
the chat. Anything we cannot send is refused loudly at upload time, never
silently dropped.
"""
from __future__ import annotations

import base64
import os

# Accepted image MIME types — the intersection Anthropic + OpenAI document.
IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
_IMAGE_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".gif": "image/gif", ".webp": "image/webp"}
PDF_MIME = "application/pdf"

# Text-like files are inlined as text. Extensions are the source of truth —
# browsers report vague/empty MIME types for most code files.
TEXT_EXTS = frozenset({
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml",
    ".xml", ".html", ".htm", ".css", ".scss", ".js", ".jsx", ".ts", ".tsx", ".mjs",
    ".cjs", ".py", ".sql", ".sh", ".ps1", ".bat", ".ini", ".toml", ".cfg", ".conf",
    ".env", ".log", ".rtf", ".svg", ".graphql", ".proto", ".java", ".cs", ".go",
    ".rs", ".rb", ".php", ".c", ".h", ".cpp", ".hpp", ".kt", ".swift", ".r",
})
_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIMES = frozenset({"application/json", "application/xml", "application/x-yaml",
                         "application/yaml", "application/javascript", "application/sql",
                         "image/svg+xml"})

# How much of a text attachment goes into the prompt. This is not a cap on the
# file (the whole file is stored and downloadable); it is the point past which
# the note below tells the model — loudly — that the tail was not included.
TEXT_INLINE_LIMIT_CHARS = 400_000

ACCEPTED_DESCRIPTION = (
    "images (PNG, JPEG, GIF, WebP), PDF documents, and text/code files "
    "(txt, md, csv, json, yaml, xml, html, css, js/ts, py, sql, …)"
)


def classify_upload(filename: str | None, content_type: str | None) -> tuple[str, str] | None:
    """Return (kind, mime) for an accepted upload, or None when unsupported.

    kind ∈ {"image", "pdf", "text"}. The MIME returned is what is sent to the
    provider, normalised from the extension when the browser's guess is
    vague (``application/octet-stream``) or missing.
    """
    name = (filename or "").strip()
    ext = os.path.splitext(name)[1].lower()
    ct = (content_type or "").split(";")[0].strip().lower()

    if ct in IMAGE_MIMES:
        return "image", ct
    if ext in _IMAGE_EXT:
        return "image", _IMAGE_EXT[ext]
    if ct == PDF_MIME or ext == ".pdf":
        return "pdf", PDF_MIME
    if ext in TEXT_EXTS or ct in _TEXT_MIMES or any(ct.startswith(p) for p in _TEXT_MIME_PREFIXES):
        return "text", ct if ct and ct != "application/octet-stream" else "text/plain"
    return None


def human_size(n: int | None) -> str:
    n = int(n or 0)
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _text_block(filename: str, data: bytes) -> str:
    text = (data or b"").decode("utf-8", errors="replace")
    note = ""
    if len(text) > TEXT_INLINE_LIMIT_CHARS:
        omitted = len(text) - TEXT_INLINE_LIMIT_CHARS
        text = text[:TEXT_INLINE_LIMIT_CHARS]
        note = (f"\n… [NOTE: the last {omitted} characters of this file were not "
                "included in the prompt — ask the user for the specific part you need]")
    ext = os.path.splitext(filename)[1].lstrip(".").lower()
    return f"\n\n[Attached file: {filename}]\n```{ext}\n{text}{note}\n```"


def omitted_note(filename: str, kind: str, size: int | None) -> str:
    """Placeholder for an attachment from an earlier turn that is outside the
    replay window (we do not resend every screenshot of a long build)."""
    return (f"\n\n[Attachment from this earlier message not re-sent: {filename} "
            f"({kind}, {human_size(size)}) — ask the user to attach it again if you need it]")


def build_user_content(text: str, attachments: list, *, include_binary: bool = True):
    """Build the ``content`` for a user turn.

    ``attachments`` are objects with ``.filename``, ``.kind``, ``.mime``,
    ``.size`` and (when ``include_binary``) ``.data``. Returns the plain
    string when there is nothing binary to send — messages without
    attachments stay byte-identical to before — otherwise the OpenAI-format
    list: one ``text`` part followed by ``image_url`` / ``file`` parts.
    """
    body = text or ""
    parts: list[dict] = []
    for a in attachments or []:
        kind = getattr(a, "kind", "")
        name = getattr(a, "filename", "") or "attachment"
        if kind == "text":
            data = getattr(a, "data", None)
            if data is None:
                body += omitted_note(name, kind, getattr(a, "size", None))
            else:
                body += _text_block(name, data)
            continue
        if not include_binary or getattr(a, "data", None) is None:
            body += omitted_note(name, kind, getattr(a, "size", None))
            continue
        b64 = base64.b64encode(a.data).decode("ascii")
        if kind == "image":
            parts.append({"type": "image_url",
                          "image_url": {"url": f"data:{a.mime};base64,{b64}"}})
        elif kind == "pdf":
            parts.append({"type": "file",
                          "file": {"filename": name,
                                   "file_data": f"data:{PDF_MIME};base64,{b64}"}})
    if not parts:
        return body
    return [{"type": "text", "text": body or "(see attached)"}] + parts


def needs_vision(attachments: list) -> bool:
    return any(getattr(a, "kind", "") == "image" for a in attachments or [])


def needs_pdf(attachments: list) -> bool:
    return any(getattr(a, "kind", "") == "pdf" for a in attachments or [])


def meta(a) -> dict:
    """Non-binary summary of an attachment for API responses/logs."""
    return {"id": getattr(a, "id", None), "name": getattr(a, "filename", ""),
            "mime": getattr(a, "mime", ""), "kind": getattr(a, "kind", ""),
            "size": int(getattr(a, "size", 0) or 0)}
