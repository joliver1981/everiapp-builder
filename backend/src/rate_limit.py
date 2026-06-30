"""Token-bucket rate limiting, keyed by an arbitrary string (user id, app id).

Process-local and dependency-free — fits the single-server on-prem model. Used
by the AI chat handler (per-user) and the app-DB / dataset runtime (per-app).

A bucket refills at `rate` tokens/second up to `capacity`. Each request costs
1 token. When empty, the request is denied (caller returns 429).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class RateLimiter:
    def __init__(self, rate_per_sec: float, capacity: int):
        self.rate = rate_per_sec
        self.capacity = capacity
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, cost: float = 1.0) -> bool:
        now = time.monotonic()
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                b = _Bucket(tokens=float(self.capacity), last_refill=now)
                self._buckets[key] = b
            # Refill
            elapsed = now - b.last_refill
            b.tokens = min(self.capacity, b.tokens + elapsed * self.rate)
            b.last_refill = now
            if b.tokens >= cost:
                b.tokens -= cost
                return True
            return False

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)


# Shared limiters. Defaults are generous; an admin can tighten via settings
# later. These are module-level so they persist across requests in one process.
#   - chat: 30 messages / minute / user  → 0.5/s, burst 30
#   - app-db: 120 ops / minute / app     → 2/s,   burst 120
#   - dataset: 120 calls / minute / app  → 2/s,   burst 120
chat_limiter = RateLimiter(rate_per_sec=0.5, capacity=30)
app_db_limiter = RateLimiter(rate_per_sec=2.0, capacity=120)
dataset_limiter = RateLimiter(rate_per_sec=2.0, capacity=120)
