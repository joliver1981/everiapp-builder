"""The verify probes' static server + the loud-truncation parser guard.

The boot/runtime probes used to spawn `npx --yes serve`, which DOWNLOADS the
`serve` package from registry.npmjs.org on first use — so on an offline or
firewalled install (a normal client posture) every probe failed with
"npm error ENOTFOUND registry.npmjs.org" and a green build was reported broken.
They now serve dist/ from an in-process stdlib server: these tests are the
regression lock — they run with NO network and NO node.

find_unterminated_file is the companion guard: a response truncated at an
output-length limit leaves its trailing FILE block without a closing fence,
which parse_llm_response silently drops — the downstream symptom was a baffling
TS2307 "Cannot find module" at a client site. The parser must name the casualty.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import urllib.request
from pathlib import Path

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "probe-static-test")

from src.ai import verifier  # noqa: E402
from src.ai.code_parser import find_unterminated_file  # noqa: E402


def _make_dist(root: Path) -> Path:
    dist = root / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        '<!doctype html><html><head>'
        '<script type="module" src="/assets/index-abc123.js"></script>'
        '</head><body><div id="root"></div></body></html>',
        encoding="utf-8",
    )
    (dist / "assets" / "index-abc123.js").write_text("console.log('boot')", encoding="utf-8")
    return dist


def _get(url: str) -> tuple[int, str, str]:
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read().decode("utf-8", "replace")


def test_static_server_serves_files_spa_fallback_and_js_mime(tmp_path):
    dist = _make_dist(tmp_path)
    httpd, thread, port = verifier._start_static_server(dist)
    try:
        status, ctype, body = _get(f"http://127.0.0.1:{port}/")
        assert status == 200 and "root" in body

        status, ctype, body = _get(f"http://127.0.0.1:{port}/assets/index-abc123.js")
        assert status == 200
        # Chromium refuses module scripts without a JS MIME — pinned in the
        # handler so a broken Windows registry mapping can't break the probe.
        assert "javascript" in ctype

        # SPA fallback: an extension-less client-side route serves index.html.
        status, _, body = _get(f"http://127.0.0.1:{port}/reports/summary")
        assert status == 200 and "root" in body
    finally:
        verifier._stop_static_server(httpd, thread)


def test_boot_probe_passes_offline(monkeypatch, tmp_path):
    """End-to-end boot probe over a healthy dist — no network, no node, no npm."""
    from src.config import settings
    monkeypatch.setattr(settings, "app_data_dir", str(tmp_path))
    app_dir = tmp_path / "probe-app" / "draft" / "frontend"
    _make_dist(app_dir)
    try:
        result = asyncio.run(verifier.run_boot_probe("probe-app"))
        assert result.passed, result.errors
    finally:
        shutil.rmtree(tmp_path / "probe-app", ignore_errors=True)


def test_boot_probe_flags_missing_bundle(monkeypatch, tmp_path):
    """The probe still catches the bug class it exists for: index.html references
    a bundle that doesn't exist → a boot error, not a pass."""
    from src.config import settings
    monkeypatch.setattr(settings, "app_data_dir", str(tmp_path))
    app_dir = tmp_path / "probe-app-404" / "draft" / "frontend"
    dist = _make_dist(app_dir)
    (dist / "assets" / "index-abc123.js").unlink()
    result = asyncio.run(verifier.run_boot_probe("probe-app-404"))
    assert not result.passed
    assert any("bundle" in e.message.lower() for e in result.errors)


# --------------------------------------------------------- truncation naming

_COMPLETE = (
    "Here you go.\n\n"
    "```tsx\n// FILE: src/App.tsx\nexport default function App() { return null }\n```\n\n"
    "```ts\n// FILE: src/lib/packing.ts\nexport const x = 1\n```\n"
)


def test_no_truncation_on_complete_response():
    assert find_unterminated_file(_COMPLETE) is None
    assert find_unterminated_file("plain prose, no files at all") is None
    assert find_unterminated_file("") is None


def test_truncated_trailing_file_is_named():
    cut = _COMPLETE + (
        "\n```tsx\n// FILE: src/components/PalletLayoutStudio.tsx\n"
        "export function PalletLayoutStudio() {\n  const rows ="  # …stream cut here
    )
    assert find_unterminated_file(cut) == "src/components/PalletLayoutStudio.tsx"


def test_truncated_python_file_header_also_detected():
    cut = "```python\n# FILE: server/functions/report.py\nimport pandas as pd\ndef run(ctx"
    assert find_unterminated_file(cut) == "server/functions/report.py"


def test_unfenced_file_mention_in_prose_is_not_a_false_positive():
    prose = "I updated `src/App.tsx`. The FILE: header convention is unchanged."
    assert find_unterminated_file(prose) is None
