"""Reviewable generation debug log.

Captures, per app-build turn, the FULL detail needed to trace a failure instead of
guessing: the user's prompt, the exact system prompts sent, the model's RAW output
(generated code), every verify error in full, and every fix attempt (the errors fed
back + the raw response + the new code). Written as JSON Lines to
`<data>/logs/generation_debug.jsonl` so it can be tailed/grepped/read directly.

Active only when `settings.debug` is on (dev), so production isn't logging code/PII.
Best-effort throughout — a logging failure must never break app generation.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from ..config import settings

logger = logging.getLogger(__name__)

_MAX_BYTES = 30 * 1024 * 1024     # rotate the file at ~30 MB
_MAX_FILE_CHARS = 10_000          # per generated file
_MAX_RAW_CHARS = 60_000           # per raw LLM response


def enabled() -> bool:
    return bool(getattr(settings, "debug", False))


def log_path() -> Path:
    d = Path(settings.app_data_dir).parent / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / "generation_debug.jsonl"


def _trunc(s: str | None, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + f"\n…[truncated {len(s) - n} chars]"


def raw(s: str | None) -> str:
    return _trunc(s, _MAX_RAW_CHARS)


def files_payload(files) -> list[dict]:
    """[{path, action, content(truncated)}] from a list of GeneratedFile-likes."""
    out = []
    for f in files or []:
        out.append({
            "path": getattr(f, "path", None),
            "action": getattr(f, "action", None),
            "content": _trunc(getattr(f, "content", ""), _MAX_FILE_CHARS),
        })
    return out


def errors_payload(errors) -> list[dict]:
    """Full (untruncated) verify errors — the part we were previously flying blind on."""
    out = []
    for e in errors or []:
        out.append({
            "stage": getattr(e, "stage", None),
            "file": getattr(e, "file", None),
            "line": getattr(e, "line", None),
            "code": getattr(e, "code", None),
            "message": getattr(e, "message", ""),
        })
    return out


def log(kind: str, **data) -> None:
    """Append one event as a JSON line. Never raises."""
    if not enabled():
        return
    try:
        p = log_path()
        try:
            if p.exists() and p.stat().st_size > _MAX_BYTES:
                p.replace(p.with_name(p.name + ".1"))
        except Exception:
            pass
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, **data}
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception:
        logger.exception("generation debug log write failed")
