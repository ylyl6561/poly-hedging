"""Tests for api.lanes — TokenBucket and LaneLimiter."""

from __future__ import annotations

import threading
import time

from api.lanes import (
    DEFAULT_LANE_CONFIGS,
    Lane,
    LaneConfig,
    LaneLimiter,
    TokenBucket,
)


def test_token_bucket_basic_refill():
    tb = TokenBucket(capacity=2.0, refill_rate=2.0)  # 2 / sec
    assert tb.try_acquire() is True
    assert tb.try_acquire() is True
    assert tb.try_acquire() is False
    # Wait long enough for refill
    time.sleep(0.6)
    assert tb.try_acquire() is True


def test_token_bucket_capacity_cap():
    # Even after a long pause we never exceed capacity
    tb = TokenBucket(capacity=3.0, refill_rate=10.0)
    time.sleep(0.5)
    # should cap at 3
    assert sum(tb.try_acquire() for _ in range(10)) == 3


def test_token_bucket_blocking_acquire():
    # Start with capacity consumed so the next acquire must block.
    tb = TokenBucket(capacity=1.0, refill_rate=10.0)  # 100ms / token
    assert tb.try_acquire() is True  # drain capacity
    assert tb.try_acquire() is False
    start = time.monotonic()
    ok = tb.acquire(timeout=0.5)
    elapsed = time.monotonic() - start
    assert ok is True
    assert 0.05 <= elapsed <= 0.4  # bounded by capacity*refill interval


def test_token_bucket_timeout():
    # Capacity exhausted; refill_rate slow enough that timeout kicks in.
    tb = TokenBucket(capacity=1.0, refill_rate=0.5)  # 2 sec / token
    assert tb.try_acquire() is True  # drain capacity
    start = time.monotonic()
    ok = tb.acquire(timeout=0.2)
    elapsed = time.monotonic() - start
    assert ok is False
    assert elapsed < 0.4  # we shouldn't wait long after timeout


def test_token_bucket_invalid_args():
    import pytest

    with pytest.raises(ValueError):
        TokenBucket(capacity=0, refill_rate=1)
    with pytest.raises(ValueError):
        TokenBucket(capacity=1, refill_rate=0)


def test_lane_limiter_three_lanes_independent():
    lim = LaneLimiter(
        overrides={
            Lane.DATA_READ: LaneConfig(capacity=1, refill_rate=0.1),
            Lane.TRADE_WRITE: LaneConfig(capacity=10, refill_rate=10),
            Lane.WARMUP: LaneConfig(capacity=1, refill_rate=0.1),
        }
    )
    # Exhaust DATA_READ
    assert lim.try_acquire(Lane.DATA_READ) is True
    assert lim.try_acquire(Lane.DATA_READ) is False
    # TRADE_WRITE still healthy
    assert all(lim.try_acquire(Lane.TRADE_WRITE) for _ in range(10))
    # WARMUP also separate
    assert lim.try_acquire(Lane.WARMUP) is True
    assert lim.try_acquire(Lane.WARMUP) is False


def test_lane_limiter_trade_write_isolated_from_data_read():
    """Critical guarantee: read lane exhaustion must NOT block writes."""
    cfg = {Lane.DATA_READ: LaneConfig(capacity=1, refill_rate=0.01)}
    lim = LaneLimiter(overrides=cfg)
    # Drain data-read
    assert lim.try_acquire(Lane.DATA_READ) is True
    assert lim.try_acquire(Lane.DATA_READ) is False
    # Trade-write still goes through
    assert lim.try_acquire(Lane.TRADE_WRITE) is True


def test_lane_limiter_default_configs_have_sensible_defaults():
    # Sanity-check defaults — at least one of each lane exists.
    assert Lane.TRADE_WRITE in DEFAULT_LANE_CONFIGS
    assert Lane.DATA_READ in DEFAULT_LANE_CONFIGS
    assert Lane.WARMUP in DEFAULT_LANE_CONFIGS
    # Trade-write should refill faster than warmup (so writes are never throttled
    # by a backlog of reads).
    assert (
        DEFAULT_LANE_CONFIGS[Lane.TRADE_WRITE].refill_rate
        > DEFAULT_LANE_CONFIGS[Lane.WARMUP].refill_rate
    )


def test_lane_limiter_unknown_raises():
    import pytest

    lim = LaneLimiter()
    with pytest.raises(KeyError):
        lim.try_acquire("not-a-lane")  # type: ignore[arg-type]


def test_lane_limiter_stats():
    lim = LaneLimiter(
        overrides={Lane.DATA_READ: LaneConfig(capacity=2, refill_rate=2)}
    )
    lim.try_acquire(Lane.DATA_READ)
    lim.try_acquire(Lane.DATA_READ)
    stats = {s.lane: s for s in lim.stats()}
    assert stats[Lane.DATA_READ].total_acquired == 2
    assert stats[Lane.TRADE_WRITE].total_acquired == 0


def test_lane_limiter_thread_safe():
    """Concurrent acquires should never exceed capacity."""
    lim = LaneLimiter(
        overrides={Lane.DATA_READ: LaneConfig(capacity=10, refill_rate=100)}
    )
    results = []
    lock = threading.Lock()

    def worker():
        for _ in range(20):
            ok = lim.try_acquire(Lane.DATA_READ)
            with lock:
                results.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # At any instant, only `capacity` tokens can be acquired without refill.
    # Since we have 100 r/s refill and 100 attempts spread over wall time,
    # the total must be <= 10 (capacity) + a small refill-driven boost.
    # We check >= capacity was acquired, and at most ~2x capacity.
    assert sum(1 for r in results if r) >= 10
    assert sum(1 for r in results if r) <= 30  # generous upper bound