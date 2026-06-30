"""In-process TTL cache for dataset query results.

Keyed by (dataset_id, sorted-params-hash, current_user). Only read-path
executes are cached; mutations never are. The cache is best-effort and
process-local — on a multi-process deployment each worker keeps its own, which
is fine for a single-server on-prem install. Disable per-dataset by setting
cache_ttl_seconds=0.

This is deliberately simple (dict + monotonic timestamps + size cap) rather
than pulling in Redis — the on-prem product ships self-contained.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any, Optional

_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, Any]] = {}  # key -> (expires_at_monotonic, value)
_MAX_ENTRIES = 1000


def _key(dataset_id: str, params: dict, current_user: str) -> str:
    raw = json.dumps({"d": dataset_id, "p": params, "u": current_user},
                     sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def get(dataset_id: str, params: dict, current_user: str) -> Optional[Any]:
    k = _key(dataset_id, params, current_user)
    now = time.monotonic()
    with _LOCK:
        entry = _CACHE.get(k)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at < now:
            _CACHE.pop(k, None)
            return None
        return value


def put(dataset_id: str, params: dict, current_user: str, value: Any, ttl_seconds: int) -> None:
    if ttl_seconds <= 0:
        return
    k = _key(dataset_id, params, current_user)
    with _LOCK:
        # Cheap size cap: if we're full, drop the oldest-expiring entries.
        if len(_CACHE) >= _MAX_ENTRIES:
            for old in sorted(_CACHE, key=lambda kk: _CACHE[kk][0])[:_MAX_ENTRIES // 10]:
                _CACHE.pop(old, None)
        _CACHE[k] = (time.monotonic() + ttl_seconds, value)


def invalidate_dataset(dataset_id: str) -> int:
    """Drop all cached entries for a dataset (e.g. after a mutation). Returns
    the number of entries removed. We can't reverse the hash, so we tag entries
    — simplest correct approach is to clear everything matching the dataset id
    prefix, which we don't store, so we conservatively clear the whole cache
    when a dataset is mutated. For a single-server install this is acceptable.
    """
    with _LOCK:
        n = len(_CACHE)
        _CACHE.clear()
        return n


def clear() -> None:
    with _LOCK:
        _CACHE.clear()


def stats() -> dict[str, int]:
    with _LOCK:
        return {"entries": len(_CACHE), "max_entries": _MAX_ENTRIES}
