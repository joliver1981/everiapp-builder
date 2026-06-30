"""Run the LLM analysis on a bug report and return a structured AnalysisResult."""
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai_providers.service import ai_provider_service
from ..config import settings
from .prompts import ANALYZER_SYSTEM_PROMPT, build_analyzer_user_prompt
from ..ai_prompts import registry as prompt_registry

logger = logging.getLogger(__name__)


# Files in the app source tree we send to the LLM. Keep the prompt budget sane.
_INCLUDE_GLOBS = ("*.ts", "*.tsx", "*.js", "*.jsx", "*.css", "*.html", "*.json")
_EXCLUDE_DIR_NAMES = {"node_modules", "dist", ".git", "build"}
_EXCLUDE_FILE_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml"}
_MAX_FILE_BYTES = 60 * 1024  # skip enormous files
_MAX_TOTAL_BYTES = 350 * 1024  # ~85k tokens worth of source — generous but not unbounded


@dataclass
class AnalysisResult:
    diagnosis: str = ""
    root_cause: str = ""
    proposed_files: list[dict] = field(default_factory=list)
    risk_level: str = "medium"
    risk_rationale: str = ""
    raw_response: str = ""
    llm_model: str | None = None
    error: str | None = None


def _resolve_source_dir(app_id: str, version: int | None) -> Path | None:
    base = Path(settings.app_data_dir).resolve() / app_id
    if version is not None:
        candidate = base / "versions" / f"v{version}"
        if candidate.exists():
            return candidate
    # Fall back to draft when no version (or version dir missing).
    candidate = base / "draft" / "frontend"
    return candidate if candidate.exists() else None


def _collect_files(source_dir: Path) -> list[dict]:
    """Walk the source dir and return [{path, content}] for files we can send to the LLM."""
    out: list[dict] = []
    total = 0
    for root in source_dir.rglob("*"):
        if not root.is_file():
            continue
        # exclude any path component in the blacklist
        if any(part in _EXCLUDE_DIR_NAMES for part in root.parts):
            continue
        if root.name in _EXCLUDE_FILE_NAMES:
            continue
        if not any(root.match(g) for g in _INCLUDE_GLOBS):
            continue
        try:
            size = root.stat().st_size
        except OSError:
            continue
        if size > _MAX_FILE_BYTES:
            continue
        if total + size > _MAX_TOTAL_BYTES:
            # Stop once we'd blow the budget. We've already captured the most-prefixed files first.
            break
        try:
            content = root.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(root.relative_to(source_dir)).replace("\\", "/")
        out.append({"path": rel, "content": content})
        total += size
    # Prefer src/ files first (most likely culprits) when budget is tight.
    out.sort(key=lambda f: (0 if f["path"].startswith("src/") else 1, f["path"]))
    return out


def parse_analyzer_response(raw: str) -> AnalysisResult:
    """Parse the LLM's structured JSON response into an AnalysisResult.

    Tolerates the response being wrapped in markdown fences, having leading/trailing prose,
    and uses sensible defaults when fields are missing.
    """
    result = AnalysisResult(raw_response=raw)

    json_str = _extract_json_block(raw)
    if not json_str:
        result.error = "LLM response did not contain a JSON block"
        return result

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        result.error = f"LLM response was not valid JSON: {e}"
        return result

    result.diagnosis = str(data.get("diagnosis", "")).strip()
    result.root_cause = str(data.get("root_cause", "")).strip()
    result.risk_rationale = str(data.get("risk_rationale", "")).strip()

    risk = str(data.get("risk_level", "medium")).strip().lower()
    if risk not in ("low", "medium", "high"):
        risk = "high"  # unknown → treat as high so it doesn't auto-deploy
    result.risk_level = risk

    files = data.get("proposed_files", [])
    if isinstance(files, list):
        cleaned = []
        for f in files:
            if not isinstance(f, dict):
                continue
            path = str(f.get("path", "")).strip()
            action = str(f.get("action", "update")).strip().lower()
            content = str(f.get("content", ""))
            if not path:
                continue
            if action not in ("create", "update", "delete"):
                action = "update"
            # Defensive: reject any path that tries to escape the app sandbox.
            if path.startswith("/") or ".." in path.split("/"):
                continue
            cleaned.append({"path": path, "action": action, "content": content})
        result.proposed_files = cleaned

    return result


def _extract_json_block(raw: str) -> str | None:
    # Prefer ```json fences
    m = re.search(r"```json\s*\n(.*?)\n```", raw, re.DOTALL)
    if m:
        return m.group(1)
    # Fall back to any fenced block
    m = re.search(r"```\s*\n(\{.*?\})\n```", raw, re.DOTALL)
    if m:
        return m.group(1)
    # Last resort: first brace-balanced object in the response
    start = raw.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(raw)):
        c = raw[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return raw[start:i + 1]
    return None


async def run_analysis(
    db: AsyncSession,
    *,
    app_id: str,
    version: int | None,
    bug_title: str,
    bug_description: str,
    captured_context: dict,
    extra_note: str = "",
    provider_id: str | None = None,
) -> AnalysisResult:
    """Top-level entry — gathers source, calls the LLM, parses, returns."""
    source_dir = _resolve_source_dir(app_id, version)
    if not source_dir:
        return AnalysisResult(error="App source directory not found on disk")

    files = _collect_files(source_dir)
    if not files:
        return AnalysisResult(error="No analyzable source files found")

    # Pick the configured generation provider (same one the chat builder uses).
    if provider_id:
        provider_config = await ai_provider_service.get_provider_config(db, provider_id)
    else:
        provider_config = await ai_provider_service.get_default_provider_config(db, purpose="generation")
    if not provider_config:
        return AnalysisResult(error="No AI provider configured. Set one in Admin → AI Providers.")

    user_prompt = build_analyzer_user_prompt(
        bug_title=bug_title,
        bug_description=bug_description,
        captured_context=captured_context,
        files=files,
        version=version,
        extra_note=extra_note,
    )

    try:
        from ..llm_compat import acompletion

        provider_type = provider_config["provider_type"]
        model = provider_config["model"]
        llm_model = model if provider_type == "openai" else f"{provider_type}/{model}"

        response = await acompletion(
            model=llm_model,
            messages=[
                {"role": "system", "content": await prompt_registry.resolve(db, "analyzer_system_prompt")},
                {"role": "user", "content": user_prompt},
            ],
            api_key=provider_config["api_key"],
            base_url=provider_config.get("base_url"),
            max_tokens=8192,
            temperature=0.2,  # deterministic; we want surgical fixes
            stream=False,
        )
        raw = response.choices[0].message.content or ""
        result = parse_analyzer_response(raw)
        result.llm_model = llm_model
        return result
    except Exception as e:
        logger.exception("Bug-report analyzer failed for app %s", app_id)
        return AnalysisResult(error=f"LLM call failed: {type(e).__name__}: {e}")
