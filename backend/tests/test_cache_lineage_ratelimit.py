"""Tests for the query cache, data lineage, and rate limiter."""
from __future__ import annotations

import time

import pytest


# --- query cache -----------------------------------------------------------
def test_cache_put_get_roundtrip():
    from src.datasets import cache
    cache.clear()
    cache.put("ds1", {"a": 1}, "alice", {"rows": [1, 2]}, ttl_seconds=10)
    got = cache.get("ds1", {"a": 1}, "alice")
    assert got == {"rows": [1, 2]}


def test_cache_keys_by_user():
    from src.datasets import cache
    cache.clear()
    cache.put("ds1", {}, "alice", "ALICE_DATA", ttl_seconds=10)
    cache.put("ds1", {}, "bob", "BOB_DATA", ttl_seconds=10)
    assert cache.get("ds1", {}, "alice") == "ALICE_DATA"
    assert cache.get("ds1", {}, "bob") == "BOB_DATA"


def test_cache_keys_by_params():
    from src.datasets import cache
    cache.clear()
    cache.put("ds1", {"n": 1}, "alice", "ONE", ttl_seconds=10)
    cache.put("ds1", {"n": 2}, "alice", "TWO", ttl_seconds=10)
    assert cache.get("ds1", {"n": 1}, "alice") == "ONE"
    assert cache.get("ds1", {"n": 2}, "alice") == "TWO"


def test_cache_expires():
    from src.datasets import cache
    cache.clear()
    cache.put("ds1", {}, "alice", "DATA", ttl_seconds=0)  # 0 = don't cache
    assert cache.get("ds1", {}, "alice") is None


def test_cache_invalidate_clears():
    from src.datasets import cache
    cache.clear()
    cache.put("ds1", {}, "alice", "DATA", ttl_seconds=10)
    cache.invalidate_dataset("ds1")
    assert cache.get("ds1", {}, "alice") is None


def test_cache_ttl_zero_is_noop():
    from src.datasets import cache
    cache.clear()
    cache.put("ds1", {}, "alice", "DATA", ttl_seconds=0)
    assert cache.stats()["entries"] == 0


# --- rate limiter ----------------------------------------------------------
def test_rate_limiter_allows_within_burst():
    from src.rate_limit import RateLimiter
    rl = RateLimiter(rate_per_sec=1.0, capacity=5)
    # 5 immediate allows (full bucket)
    assert all(rl.allow("app1") for _ in range(5))
    # 6th denied (bucket empty, no time to refill)
    assert rl.allow("app1") is False


def test_rate_limiter_refills_over_time():
    from src.rate_limit import RateLimiter
    rl = RateLimiter(rate_per_sec=100.0, capacity=2)
    assert rl.allow("app1")
    assert rl.allow("app1")
    assert rl.allow("app1") is False
    time.sleep(0.05)  # 100/s → ~5 tokens in 50ms, capped at 2
    assert rl.allow("app1") is True


def test_rate_limiter_keys_independent():
    from src.rate_limit import RateLimiter
    rl = RateLimiter(rate_per_sec=1.0, capacity=1)
    assert rl.allow("appA")
    assert rl.allow("appA") is False
    assert rl.allow("appB")  # different key, fresh bucket
