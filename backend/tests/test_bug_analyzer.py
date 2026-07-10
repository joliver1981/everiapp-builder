"""Unit tests for bug-analyzer source collection and fix-proposal hygiene
(pure, no DB/LLM).

Locks in the two fixes to _collect_files: relevance ordering is applied BEFORE
the byte budget (so truncation drops the least-relevant files, not whatever the
filesystem walk reached first), and an oversized file is HEAD-truncated rather
than skipped (the big component is often exactly where the bug lives).

Also locks in the platform-owned-file guard: the analyzer once diagnosed a real
platform bug (useAppSchema's version-1 collision) and proposed the fix in
src/sdk/useAppDB.ts — the vendored SDK, which the platform re-vendors on every
preview start. Applying it would have silently evaporated. Such proposals are
stripped at parse time and refused at apply time.
"""
from __future__ import annotations

import json

import pytest

from src.bug_reports import analyzer


def test_collect_files_orders_src_first_before_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(analyzer, "_MAX_FILE_BYTES", 100)
    monkeypatch.setattr(analyzer, "_MAX_TOTAL_BYTES", 250)

    (tmp_path / "src").mkdir()
    (tmp_path / "public").mkdir()
    # A non-src file that a filesystem walk might reach first, and two src files
    # big enough that the budget is exhausted by src/ alone.
    (tmp_path / "public" / "extra.js").write_text("x" * 200, encoding="utf-8")
    (tmp_path / "src" / "App.tsx").write_text("A" * 200, encoding="utf-8")   # oversized
    (tmp_path / "src" / "b.tsx").write_text("B" * 200, encoding="utf-8")

    files = analyzer._collect_files(tmp_path)
    paths = [f["path"] for f in files]

    # src/ files win the budget; the non-src file is what gets dropped.
    assert all(p.startswith("src/") for p in paths), paths
    assert "public/extra.js" not in paths
    assert "src/App.tsx" in paths


def test_collect_files_head_truncates_oversized_file(tmp_path, monkeypatch):
    monkeypatch.setattr(analyzer, "_MAX_FILE_BYTES", 100)
    monkeypatch.setattr(analyzer, "_MAX_TOTAL_BYTES", 10_000)

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "Huge.tsx").write_text("Z" * 5000, encoding="utf-8")

    files = analyzer._collect_files(tmp_path)
    huge = next(f for f in files if f["path"] == "src/Huge.tsx")
    # Included (not skipped) but head-truncated with a marker.
    assert "file truncated for length" in huge["content"]
    assert len(huge["content"]) < 5000


def test_is_platform_owned_path():
    for p in (
        "src/sdk/useAppDB.ts",
        "src/sdk/nested/deep.ts",
        "./src/sdk/index.ts",
        "src\\sdk\\useAppDB.ts",   # windows separators normalized
        "package.json",
        "package-lock.json",
        "vite.config.ts",
        "tsconfig.json",
        "index.html",
        "src/main.tsx",
    ):
        assert analyzer.is_platform_owned_path(p), p
    for p in (
        "src/App.tsx",
        "src/components/Dashboard.tsx",
        "src/hooks/useHistory.ts",
        "src/types/index.ts",
        "decisions.json",
        "src/sdk.ts",               # not under src/sdk/
    ):
        assert not analyzer.is_platform_owned_path(p), p


def test_is_platform_owned_path_rejects_evasive_spellings():
    """Non-canonical spellings of the same on-disk file must not slip past:
    pathlib's resolve() at apply time collapses all of these onto the real
    platform file, so the guard has to collapse them identically (adversarial
    review found each of these bypassed the original string comparison)."""
    for p in (
        "src//sdk/useAppDB.ts",       # doubled separator
        "src/./sdk/useAppDB.ts",      # interior ./
        "./src/./sdk/useAppDB.ts",
        "SRC/SDK/useAppDB.ts",        # NTFS/APFS are case-insensitive
        "Src/sdk/USEAPPDB.TS",
        "Package.json",
        "package.json.",              # Windows strips trailing dots…
        "package.json ",              # …and trailing spaces
        "src/sdk/../sdk/useAppDB.ts", # still inside src/sdk after collapse
    ):
        assert analyzer.is_platform_owned_path(p), p


def test_write_fix_files_refuses_platform_owned_even_when_evasive(tmp_path):
    """Apply-time is the load-bearing guard: legacy stored analyses (and any
    parse bypass) reach it, and it must refuse the write AND report the skip
    so a partial apply can't wear an unqualified success status."""
    from src.bug_reports.service import BugReportsService

    draft = tmp_path / "frontend"
    (draft / "src" / "sdk").mkdir(parents=True)
    (draft / "src" / "sdk" / "useAppDB.ts").write_text("VENDORED", encoding="utf-8")
    (draft / "package.json").write_text('{"name":"app"}', encoding="utf-8")

    skipped = BugReportsService._write_fix_files(draft.resolve(), [
        {"path": "src//sdk/useAppDB.ts", "action": "update", "content": "// evil"},
        {"path": "SRC/SDK/useAppDB.ts", "action": "update", "content": "// evil"},
        {"path": "package.json.", "action": "update", "content": "{}"},
        {"path": "src/sdk/useAppDB.ts", "action": "delete", "content": ""},
        {"path": "src/components/Fix.tsx", "action": "create", "content": "// legit fix"},
    ])

    # Platform files untouched — not overwritten, not deleted.
    assert (draft / "src" / "sdk" / "useAppDB.ts").read_text(encoding="utf-8") == "VENDORED"
    assert (draft / "package.json").read_text(encoding="utf-8") == '{"name":"app"}'
    # The legitimate app-level change landed.
    assert (draft / "src" / "components" / "Fix.tsx").read_text(encoding="utf-8") == "// legit fix"
    # Every refused path is reported for the fix attempt's record.
    assert len(skipped) == 4
    assert "src/components/Fix.tsx" not in skipped


def test_parse_strips_platform_owned_proposals_and_raises_risk():
    """The live scenario: the analyzer proposes an update to the vendored SDK
    alongside a legitimate app-file change. The SDK proposal must be stripped,
    the remaining fix marked high-risk, and the rationale must say why."""
    payload = {
        "diagnosis": "useAppSchema migration identity collision",
        "root_cause": "src/sdk/useAppDB.ts always sends version 1 / name app_schema",
        "proposed_files": [
            {"path": "src/sdk/useAppDB.ts", "action": "update", "content": "// patched sdk"},
            {"path": "src/hooks/useHistory.ts", "action": "update", "content": "// workaround"},
        ],
        "risk_level": "medium",
        "risk_rationale": "SDK change affects all data access.",
    }
    result = analyzer.parse_analyzer_response(f"```json\n{json.dumps(payload)}\n```")

    paths = [f["path"] for f in result.proposed_files]
    assert "src/sdk/useAppDB.ts" not in paths
    assert "src/hooks/useHistory.ts" in paths          # app-level part survives
    assert result.risk_level == "high"                 # partial fix → never auto-apply
    assert "src/sdk/useAppDB.ts" in result.risk_rationale
    assert "platform" in result.risk_rationale.lower()


def test_analyzer_prompt_declares_platform_owned_files():
    """The prompt must teach the model that src/sdk/ + scaffold files are the
    platform's, that a root cause there is a platform bug needing a platform
    update, and that it must never route the user to edit them via the builder."""
    from src.bug_reports.prompts import ANALYZER_SYSTEM_PROMPT
    p = ANALYZER_SYSTEM_PROMPT.lower()
    assert "src/sdk" in p
    assert "platform-owned" in p
    assert "platform update" in p
    assert "never" in p and "re-vendor" in p
