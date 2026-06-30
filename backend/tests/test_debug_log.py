"""Generation debug log — writes JSONL when debug is on, silent otherwise."""
from __future__ import annotations

import json
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "debug-log-test")

from src.ai import debug_log  # noqa: E402
from src.config import settings  # noqa: E402


class _File:
    def __init__(self, path, content, action="create"):
        self.path = path
        self.content = content
        self.action = action


class _Err:
    def __init__(self, message, stage="tsc"):
        self.message = message
        self.stage = stage
        self.file = None
        self.line = None
        self.code = None


def test_writes_jsonl_when_debug_on(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "debug", True)
    monkeypatch.setattr(settings, "app_data_dir", str(tmp_path / "apps"))
    debug_log.log("turn_start", app_id="a1", user_message="build x", system_prompts=["sys A"])
    debug_log.log("verify", app_id="a1", iteration=0, passed=False,
                  errors=debug_log.errors_payload([_Err("TS2322 boom")]))
    p = debug_log.log_path()
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    rec0 = json.loads(lines[0])
    assert rec0["kind"] == "turn_start" and rec0["app_id"] == "a1"
    rec1 = json.loads(lines[1])
    assert rec1["errors"][0]["message"] == "TS2322 boom"


def test_silent_when_debug_off(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "app_data_dir", str(tmp_path / "apps2"))
    debug_log.log("turn_start", app_id="a2")
    assert not (tmp_path / "logs" / "generation_debug.jsonl").exists()


def test_files_payload_truncates_and_keeps_path():
    big = "x" * 50_000
    out = debug_log.files_payload([_File("src/App.tsx", big)])
    assert out[0]["path"] == "src/App.tsx"
    assert "truncated" in out[0]["content"]
    assert len(out[0]["content"]) < 20_000


def test_raw_truncation():
    assert debug_log.raw("short") == "short"
    assert "truncated" in debug_log.raw("y" * 200_000)
