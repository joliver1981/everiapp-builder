"""Run the LLM analysis on a bug report and return a structured AnalysisResult."""
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai_providers.service import ai_provider_service
from ..config import settings
from .prompts import ANALYZER_SYSTEM_PROMPT, build_analyzer_user_prompt
from ..ai_prompts import registry as prompt_registry

logger = logging.getLogger(__name__)


# Files the PLATFORM owns inside every generated app. The vendored SDK under
# src/sdk/ is re-vendored from the template on every preview start (an edit
# there silently evaporates), and the scaffold files are pinned by the
# generation contract. A bug whose fix lives here is a PLATFORM bug — the
# analyzer prompt says so, this module strips such proposals at parse time,
# and the apply step refuses to write them (three layers, because the middle
# one is an LLM following instructions).
_PLATFORM_OWNED_PREFIXES = ("src/sdk/",)
_PLATFORM_OWNED_FILES = {
    "package.json", "package-lock.json", "vite.config.ts", "tsconfig.json",
    "index.html", "src/main.tsx", "server/sdk.py",
}


def is_platform_owned_path(path: str) -> bool:
    """True for files the app must never change (vendored SDK + scaffold).

    Canonicalizes before comparing: the same on-disk file can be spelled many
    ways — `src//sdk/x.ts`, `src/./sdk/x.ts`, `SRC/SDK/x.ts` (NTFS and APFS
    are case-insensitive), `package.json.` (Windows strips trailing dots and
    spaces). pathlib's resolve() at apply time collapses all of those onto the
    real file, so this guard must collapse them the same way or it can be
    bypassed by a non-canonical spelling.
    """
    parts = PurePosixPath(path.replace("\\", "/")).parts
    norm = [seg.rstrip(". ").lower() for seg in parts if seg not in (".", "/")]
    canon = "/".join(norm)
    return canon in _PLATFORM_OWNED_FILES or canon.startswith(_PLATFORM_OWNED_PREFIXES)


# Files in the app source tree we send to the LLM. Keep the prompt budget sane.
_INCLUDE_GLOBS = ("*.ts", "*.tsx", "*.js", "*.jsx", "*.css", "*.html", "*.json")
_EXCLUDE_DIR_NAMES = {"node_modules", "dist", ".git", "build"}
_EXCLUDE_FILE_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml"}
_MAX_FILE_BYTES = 256 * 1024   # per-file cap; oversize files are HEAD-truncated, not skipped
_MAX_TOTAL_BYTES = 768 * 1024  # ~190k chars of source — generous for a 200k-context model


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
    """Walk the source dir and return [{path, content}] for files we send to the LLM.

    Files are ordered src/ first (the most likely culprits) BEFORE the byte budget
    is applied, so when a large app would blow the budget we keep the most-relevant
    files rather than whatever filesystem-walk order happened to reach first (the
    old code sorted AFTER truncating, so it dropped the wrong files). A single
    oversized file is HEAD-truncated with a marker rather than skipped outright —
    the big component is often exactly where the bug lives, so it must not be
    invisible to the analyzer.
    """
    def _rel(p: Path) -> str:
        return str(p.relative_to(source_dir)).replace("\\", "/")

    candidates: list[Path] = []
    for root in source_dir.rglob("*"):
        if not root.is_file():
            continue
        if any(part in _EXCLUDE_DIR_NAMES for part in root.parts):
            continue
        if root.name in _EXCLUDE_FILE_NAMES:
            continue
        if not any(root.match(g) for g in _INCLUDE_GLOBS):
            continue
        candidates.append(root)

    # Relevance order first — src/ before everything, then by path.
    candidates.sort(key=lambda p: (0 if _rel(p).startswith("src/") else 1, _rel(p)))

    out: list[dict] = []
    total = 0
    for root in candidates:
        if total >= _MAX_TOTAL_BYTES:
            break
        try:
            content = root.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        cap = min(_MAX_FILE_BYTES, _MAX_TOTAL_BYTES - total)
        if len(content) > cap:
            content = content[:cap] + "\n… (file truncated for length)"
        out.append({"path": _rel(root), "content": content})
        total += len(content)
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
        dropped_platform: list[str] = []
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
            # Platform-owned files can't be fixed at the app level — an edit to
            # the vendored SDK would be overwritten at the next preview start.
            if is_platform_owned_path(path):
                dropped_platform.append(path)
                continue
            cleaned.append({"path": path, "action": action, "content": content})
        result.proposed_files = cleaned
        if dropped_platform:
            note = (
                "Discarded proposed change(s) to platform-owned file(s) the app "
                f"cannot fix: {', '.join(dropped_platform)} — the vendored SDK/"
                "scaffold is overwritten by the platform; this needs a "
                "platform-level fix, not an app fix."
            )
            result.risk_rationale = (
                f"{result.risk_rationale} {note}".strip() if result.risk_rationale else note
            )
            result.risk_level = "high"  # what's left is at best a partial fix

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
    # The copilot reuses this pipeline for on-demand diagnosis: it meters
    # under its own purpose, attributed to the requesting developer instead
    # of the background "(system)" identity.
    usage_purpose: str = "bug_analysis",
    usage_user: str = "(system)",
) -> AnalysisResult:
    """Top-level entry — gathers source, calls the LLM, parses, returns."""
    source_dir = _resolve_source_dir(app_id, version)
    if not source_dir:
        return AnalysisResult(error="App source directory not found on disk")

    files = _collect_files(source_dir)
    if not files:
        return AnalysisResult(error="No analyzable source files found")

    # Own purpose so an admin can pin a cheaper model for analysis; unpinned it
    # inherits the generation default (same provider the chat builder uses).
    if provider_id:
        provider_config = await ai_provider_service.get_provider_config(db, provider_id)
    else:
        provider_config = await ai_provider_service.get_default_provider_config(db, purpose=usage_purpose)
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

        from ..platform_settings.service import get_output_cap
        response = await acompletion(
            model=llm_model,
            messages=[
                {"role": "system", "content": await prompt_registry.resolve(db, "analyzer_system_prompt")},
                {"role": "user", "content": user_prompt},
            ],
            api_key=provider_config["api_key"],
            base_url=provider_config.get("base_url"),
            max_tokens=await get_output_cap(db, "bug_analysis_max_output_tokens"),
            temperature=0.2,  # deterministic; we want surgical fixes
            stream=False,
            aihub_span={"app_id": app_id, "user_id": usage_user,
                        "purpose": usage_purpose,
                        "provider_type": provider_type, "model": model},
        )
        raw = response.choices[0].message.content or ""

        # Cost meter (best-effort). The analyzer runs as a background system
        # task — bug reports can be anonymous — so spend is attributed to
        # "(system)" rather than a user.
        try:
            from ..llm_usage.service import record_usage
            usage = getattr(response, "usage", None)
            await record_usage(
                db, user_id=usage_user, app_id=app_id,
                provider_type=provider_type, model=model, purpose=usage_purpose,
                input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                output_tokens=getattr(usage, "completion_tokens", 0) or max(1, len(raw) // 4),
            )
        except Exception:
            # A failed commit leaves the session pending-rollback; restore it —
            # the bug_reports service keeps using this session afterwards.
            try:
                await db.rollback()
            except Exception:
                pass

        result = parse_analyzer_response(raw)
        result.llm_model = llm_model
        return result
    except Exception as e:
        logger.exception("Bug-report analyzer failed for app %s", app_id)
        try:
            from ..llm_usage.service import record_usage
            await record_usage(
                db, user_id=usage_user, app_id=app_id,
                provider_type=provider_config.get("provider_type") or "",
                model=provider_config.get("model") or "",
                purpose=usage_purpose, input_tokens=0, output_tokens=0,
                error=f"{type(e).__name__}: {e}",
            )
        except Exception:
            try:
                await db.rollback()
            except Exception:
                pass
        return AnalysisResult(error=f"LLM call failed: {type(e).__name__}: {e}")
