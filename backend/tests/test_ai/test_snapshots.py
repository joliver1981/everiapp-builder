"""Unit tests for LKG (last-known-good) snapshots.

Snapshots back up the draft directory before an AI turn so the user can
roll back if the AI's changes don't verify. We test without touching the
real data dir by pointing app_data_dir at a tmp_path via monkeypatch.
"""
from pathlib import Path

import pytest

from src.ai import snapshots
from src.config import settings


@pytest.fixture
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_data_dir", str(tmp_path))
    return tmp_path


def _make_draft(data_dir: Path, app_id: str, files: dict[str, str]) -> Path:
    """Create a draft/frontend tree with the given files."""
    draft = data_dir / app_id / "draft" / "frontend"
    draft.mkdir(parents=True)
    for rel, content in files.items():
        p = draft / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return draft


def test_snapshot_then_restore_round_trip(isolated_data):
    draft = _make_draft(isolated_data, "app1", {
        "src/App.tsx": "export default function App() { return <div>v1</div> }",
        "src/lib/utils.ts": "export const x = 1",
    })

    assert snapshots.snapshot("app1", note="initial") is True
    assert snapshots.has_snapshot("app1") is True

    # Mutate draft as if the AI applied a (broken) patch.
    (draft / "src/App.tsx").write_text("BROKEN", encoding="utf-8")
    (draft / "src/new-file.ts").write_text("added by AI", encoding="utf-8")

    assert snapshots.restore("app1") is True
    assert (draft / "src/App.tsx").read_text(encoding="utf-8").startswith("export default")
    # The AI-added file should be gone after restore (true revert, not merge)
    assert not (draft / "src/new-file.ts").exists()
    # And the original lib file is preserved
    assert (draft / "src/lib/utils.ts").read_text(encoding="utf-8") == "export const x = 1"


def test_snapshot_overwrites_previous(isolated_data):
    draft = _make_draft(isolated_data, "app2", {"a.txt": "first"})
    snapshots.snapshot("app2", note="first")

    (draft / "a.txt").write_text("second", encoding="utf-8")
    snapshots.snapshot("app2", note="second")

    # Restore should give us "second", not "first"
    (draft / "a.txt").write_text("changed-again", encoding="utf-8")
    snapshots.restore("app2")
    assert (draft / "a.txt").read_text(encoding="utf-8") == "second"


def test_snapshot_skips_node_modules(isolated_data):
    draft = _make_draft(isolated_data, "app3", {"src/App.tsx": "x"})
    (draft / "node_modules").mkdir()
    (draft / "node_modules" / "fake-lib.txt").write_text("garbage" * 100)

    snapshots.snapshot("app3")
    snap_dir = isolated_data / "app3" / "draft_lkg" / "frontend"
    assert (snap_dir / "src/App.tsx").exists()
    assert not (snap_dir / "node_modules").exists()


def test_restore_returns_false_when_no_snapshot(isolated_data):
    _make_draft(isolated_data, "app4", {"a.txt": "hi"})
    assert snapshots.has_snapshot("app4") is False
    assert snapshots.restore("app4") is False


def test_snapshot_info_returns_metadata(isolated_data):
    _make_draft(isolated_data, "app5", {"a.txt": "hi"})
    snapshots.snapshot("app5", note="make hero bigger")
    info = snapshots.snapshot_info("app5")
    assert info is not None
    assert "taken_at" in info
    assert info["note"] == "make hero bigger"


def test_clear_removes_snapshot(isolated_data):
    _make_draft(isolated_data, "app6", {"a.txt": "hi"})
    snapshots.snapshot("app6")
    assert snapshots.has_snapshot("app6")
    snapshots.clear("app6")
    assert snapshots.has_snapshot("app6") is False
