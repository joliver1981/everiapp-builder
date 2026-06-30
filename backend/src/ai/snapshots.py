"""Last-known-good (LKG) snapshots of an app's draft directory.

Used by the AI self-heal loop: before applying generated files, snapshot the
draft so the user can roll back if the AI produces something broken.

One snapshot slot per app — overwritten on each AI turn. Keep-it-simple v1.
If we ever want history, swap the single slot for a ring buffer.
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings

logger = logging.getLogger(__name__)


def _draft_dir(app_id: str) -> Path:
    return Path(settings.app_data_dir).resolve() / app_id / "draft" / "frontend"


def _lkg_dir(app_id: str) -> Path:
    return Path(settings.app_data_dir).resolve() / app_id / "draft_lkg" / "frontend"


def _meta_path(app_id: str) -> Path:
    return Path(settings.app_data_dir).resolve() / app_id / "draft_lkg" / "meta.txt"


def snapshot(app_id: str, note: str = "") -> bool:
    """Copy draft/frontend → draft_lkg/frontend, overwriting the previous snapshot.

    node_modules is skipped — it's huge and reproducible from package.json.
    Returns True on success, False if draft is missing.
    """
    src = _draft_dir(app_id)
    if not src.exists():
        logger.warning("snapshot: draft dir missing for %s", app_id)
        return False

    dst = _lkg_dir(app_id)
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    dst.parent.mkdir(parents=True, exist_ok=True)

    shutil.copytree(
        src, dst,
        ignore=shutil.ignore_patterns("node_modules", "dist", ".git"),
    )

    _meta_path(app_id).write_text(
        f"taken_at={datetime.now(timezone.utc).isoformat()}\nnote={note}\n",
        encoding="utf-8",
    )
    return True


def has_snapshot(app_id: str) -> bool:
    return _lkg_dir(app_id).exists()


def snapshot_info(app_id: str) -> dict | None:
    """Returns {taken_at, note} or None if no snapshot exists."""
    if not has_snapshot(app_id):
        return None
    meta = {"taken_at": None, "note": ""}
    try:
        for line in _meta_path(app_id).read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                meta[k.strip()] = v.strip()
    except OSError:
        pass
    return meta


def restore(app_id: str) -> bool:
    """Copy draft_lkg/frontend back over draft/frontend.

    Removes anything in draft that wasn't in the snapshot — this is a true revert,
    not a merge. Returns False if no snapshot exists.

    Preserves draft's node_modules to avoid forcing a reinstall.
    """
    src = _lkg_dir(app_id)
    if not src.exists():
        return False
    dst = _draft_dir(app_id)

    # Preserve node_modules across the restore — it's expensive to rebuild
    # and not part of the snapshot anyway.
    node_modules = dst / "node_modules"
    preserve_node_modules = node_modules.exists()

    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)

    if preserve_node_modules:
        # If node_modules was preserved by ignore_patterns above, this is a no-op;
        # but in case a future version of snapshot() changes, leave the hook.
        if not (dst / "node_modules").exists() and node_modules.exists():
            shutil.move(str(node_modules), str(dst / "node_modules"))

    return True


def clear(app_id: str) -> None:
    """Drop the snapshot. Useful after a successful publish to avoid stale rollback."""
    d = _lkg_dir(app_id).parent
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# Multi-turn history (ring buffer) — powers chat undo/rewind. Distinct from the
# single LKG slot above, which the self-heal loop uses for its one-step rollback.
# ---------------------------------------------------------------------------
import json  # noqa: E402

MAX_HISTORY = 15


def _history_root(app_id: str) -> Path:
    return Path(settings.app_data_dir).resolve() / app_id / "draft_history"


def _history_entry_dir(app_id: str, seq: int) -> Path:
    return _history_root(app_id) / str(seq) / "frontend"


def _history_meta_path(app_id: str, seq: int) -> Path:
    return _history_root(app_id) / str(seq) / "meta.json"


def _history_seqs(app_id: str) -> list[int]:
    root = _history_root(app_id)
    if not root.exists():
        return []
    seqs = []
    for child in root.iterdir():
        if child.is_dir() and child.name.isdigit():
            seqs.append(int(child.name))
    return sorted(seqs)


def history_push(app_id: str, note: str = "", message_id: str | None = None) -> int | None:
    """Snapshot the current draft into the history ring buffer. Returns the seq,
    or None if there's no draft to snapshot. Prunes to the last MAX_HISTORY."""
    src = _draft_dir(app_id)
    if not src.exists():
        return None

    seqs = _history_seqs(app_id)
    seq = (seqs[-1] + 1) if seqs else 1
    dst = _history_entry_dir(app_id, seq)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("node_modules", "dist", ".git"))
    except OSError:
        logger.exception("history_push: copy failed for %s", app_id)
        return None

    _history_meta_path(app_id, seq).write_text(json.dumps({
        "seq": seq,
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "note": note[:300],
        "message_id": message_id,
    }), encoding="utf-8")

    # Prune oldest beyond the cap.
    for old in _history_seqs(app_id)[:-MAX_HISTORY] if len(seqs) + 1 > MAX_HISTORY else []:
        shutil.rmtree(_history_root(app_id) / str(old), ignore_errors=True)
    return seq


def history_list(app_id: str) -> list[dict]:
    """Newest-first list of history entries with their metadata."""
    out = []
    for seq in sorted(_history_seqs(app_id), reverse=True):
        meta = {"seq": seq, "taken_at": None, "note": "", "message_id": None}
        try:
            meta.update(json.loads(_history_meta_path(app_id, seq).read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
        out.append(meta)
    return out


def history_restore(app_id: str, seq: int) -> bool:
    """Restore the draft to a history entry. Preserves node_modules. Before
    restoring, pushes the CURRENT state so the rewind itself is undoable."""
    src = _history_entry_dir(app_id, seq)
    if not src.exists():
        return False

    # Capture current state first so a rewind can be undone.
    history_push(app_id, note=f"before rewind to #{seq}")

    dst = _draft_dir(app_id)
    node_modules = dst / "node_modules"
    preserve = node_modules.exists()
    nm_tmp = None
    if preserve:
        nm_tmp = dst.parent / "_nm_tmp"
        shutil.rmtree(nm_tmp, ignore_errors=True)
        shutil.move(str(node_modules), str(nm_tmp))

    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)

    if preserve and nm_tmp and nm_tmp.exists():
        shutil.move(str(nm_tmp), str(dst / "node_modules"))
    return True
