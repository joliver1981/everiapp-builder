"""Unit tests for the verifier's error-parsing helpers.

The parsers turn raw stdout from tsc and vite into structured `VerifyError`
objects we can feed back to the LLM. These tests don't spawn subprocesses —
they exercise just the regex / dedup logic on captured output samples.
"""
from pathlib import Path

import pytest

from src.ai.verifier import (
    VerifyError,
    _parse_build_errors,
    _parse_tsc_errors,
    errors_to_prompt_block,
    _extract_main_bundle_url,
)


def test_parse_tsc_single_error(tmp_path: Path):
    raw = (
        "src/App.tsx(12,5): error TS2304: Cannot find name 'foo'.\n"
        "Found 1 error.\n"
    )
    errs = _parse_tsc_errors(raw, app_dir=tmp_path)
    assert len(errs) == 1
    e = errs[0]
    assert e.stage == "tsc"
    assert e.file == "src/App.tsx"
    assert e.line == 12
    assert e.column == 5
    assert e.code == "TS2304"
    assert "Cannot find name" in e.message


def test_parse_tsc_multiple_errors(tmp_path: Path):
    raw = (
        "src/App.tsx(1,1): error TS6133: 'React' is declared but its value is never read.\n"
        "src/lib/foo.ts(3,15): error TS2552: Cannot find name 'bar'. Did you mean 'baz'?\n"
        "Found 2 errors in 2 files.\n"
    )
    errs = _parse_tsc_errors(raw, app_dir=tmp_path)
    assert len(errs) == 2
    assert {e.code for e in errs} == {"TS6133", "TS2552"}


def test_parse_tsc_no_errors(tmp_path: Path):
    raw = "Found 0 errors in 0 files.\n"
    assert _parse_tsc_errors(raw, app_dir=tmp_path) == []


def test_parse_build_rollup_error(tmp_path: Path):
    raw = (
        "vite v7.0.0 building for production...\n"
        "transforming...\n"
        "error during build:\n"
        'RollupError: Could not resolve "./missing" from "src/App.tsx"\n'
        "    at error (file:///.../rollup/dist/...)\n"
    )
    errs = _parse_build_errors(raw, app_dir=tmp_path)
    assert len(errs) >= 1
    assert any("Could not resolve" in e.message for e in errs)
    assert all(e.stage == "build" for e in errs)


def test_parse_build_dedups_repeated_errors(tmp_path: Path):
    raw = (
        "error during build:\n"
        'RollupError: same exact message about src/App.tsx\n'
        "error during build:\n"
        'RollupError: same exact message about src/App.tsx\n'
    )
    errs = _parse_build_errors(raw, app_dir=tmp_path)
    assert len(errs) == 1


def test_extract_main_bundle_url_finds_first_module_script():
    html = (
        '<!doctype html><html><head>'
        '<meta name="aihub-app-id" content="x">'
        '<script type="module" crossorigin src="/assets/index-abc123.js"></script>'
        '<link rel="stylesheet" href="/assets/style.css">'
        '</head><body><div id="root"></div></body></html>'
    )
    assert _extract_main_bundle_url(html) == "/assets/index-abc123.js"


def test_extract_main_bundle_url_no_script_returns_none():
    assert _extract_main_bundle_url("<html><body>no scripts</body></html>") is None


def test_errors_to_prompt_block_includes_file_and_code():
    errs = [
        VerifyError(stage="tsc", file="src/App.tsx", line=12, column=5,
                    code="TS2304", message="Cannot find name 'foo'."),
        VerifyError(stage="build", file=None, line=None, column=None, code=None,
                    message="generic build error"),
    ]
    block = errors_to_prompt_block(errs)
    assert "src/App.tsx:12:5" in block
    assert "[TS2304]" in block
    assert "Cannot find name 'foo'" in block
    assert "generic build error" in block
    # Should include guidance about producing a corrected patch
    assert "corrected patch" in block


def test_errors_to_prompt_block_empty_when_no_errors():
    assert errors_to_prompt_block([]) == ""
