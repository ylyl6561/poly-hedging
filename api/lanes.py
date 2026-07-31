"""
Lane abstraction for the Polymarket HTTP client.

Three lanes carry traffic with very different urgency / failure-cost profiles:

* ``trade-write`` — POST /order, /cancel, redeem.
  These are revenue-critical and cannot be throttled by anything the read
  pipeline did.  The lane's token bucket is sized so it can sustain ~5
  writes / second but does not block on the read lanes.

* ``data-read``   — Anything that reads from data-api / gamma-api / clob-api
  under a Cloudflare front.  Goes through TokenBucket + CircuitBreaker +
  Budget.  If Cloudflare trips, this lane falls back to cache, but it
  never affects the write lane.

* ``warmup``      — Startup pre-fetch (leaderboard backfill, market list, …).
  Runs at the lowest priority.  May be dropped entirely when the process
  is busy.

Lanes are *per process* singletons; the ``LaneLimiter`` enforces fair
serialisation across them so a runaway warmup can never starve writes.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)


class Lane(str, Enum):
    TRADE_WRITE = "trade-write"
    DATA_READ = "data-read"
    WARMUP = "warmup"


# ============================================================================
# Token bucket
# ============================================================================


class TokenBucket:
    """Thread-safe token bucket with optional blocking acquire.

    ``capacity`` tokens may be spent instantly; the bucket refills at
    ``refill_rate`` tokens per second.  When the bucket is empty, calls
    to :meth:`acquire` block (with timeout) until tokens are available.

    The implementation is intentionally small — no asyncio, no fair queue,
    just a monotonic clock + lock.  One bucket per lane is plenty.
    """

    def __init__(self, *, capacity: float, refill_rate: float) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        if refill_rate <= 0:
            raise ValueError("refill_rate must be > 0")
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate)
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()
        # Stats — cheap counters; useful for /healthz.
        self.total_acquired = 0
        self.total_wait_seconds = 0.0

    def _refill_locked(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(
                self.capacity, self._tokens + elapsed * self.refill_rate
            )
            self._last = now

    def try_acquire(self, n: float = 1.0) -> bool:
        """Non-blocking acquire.  Returns ``True`` if a token was consumed."""
        with self._lock:
            self._refill_locked()
            if self._tokens >= n:
                self._tokens -= n
                self.total_acquired += 1
                return True
            return False

    def acquire(
        self, n: float = 1.0, *, timeout: float | None = None
    ) -> bool:
        """Block until ``n`` tokens are available.  Returns ``True`` on success,
        ``False`` on timeout.  A ``timeout`` of ``None`` waits forever."""
        deadline = None if timeout is None else time.monotonic() + timeout
        waited = 0.0
        while True:
            if self.try_acquire(n):
                self.total_wait_seconds += waited
                return True
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                # Sleep until enough tokens have refilled, capped at 250ms
                # so we still react promptly to cancellation / shutdown.
                per_token = n / self.refill_rate
                sleep_for = max(0.005, min(0.25, per_token, remaining))
            else:
                per_token = n / self.refill_rate
                sleep_for = max(0.005, min(0.25, per_token))
            waited += sleep_for
            time.sleep(sleep_for)


# ============================================================================
# Per-lane limiter
# ============================================================================


@dataclass(frozen=True)
class LaneConfig:
    capacity: float
    refill_rate: float


# Sensible defaults for our actual workloads.  Tests and callers can
# override per-instance via LaneLimiter(..., overrides=...).
DEFAULT_LANE_CONFIGS: dict[Lane, LaneConfig] = {
    # 10 trades burstable, sustained ~5 / s — matches Polymarket CLOB's
    # own rate limit (orders per second per api-key).
    Lane.TRADE_WRITE: LaneConfig(capacity=10.0, refill_rate=5.0),
    # 60 burst, refill 2 / s  →  min spacing 0.5s/req, matches current
    # request_min_interval_seconds = 0.5 default.
    Lane.DATA_READ: LaneConfig(capacity=60.0, refill_rate=2.0),
    # Warmup is throttled harder; if it backs up we just drop the work.
    Lane.WARMUP: LaneConfig(capacity=5.0, refill_rate=0.5),
}


@dataclass
class LaneStats:
    lane: Lane
    tokens: float
    capacity: float
    total_acquired: int
    total_wait_seconds: float


class LaneLimiter:
    """Owns the three lane buckets and exposes a uniform ``acquire`` API.

    ``acquire(lane, block=True)`` either returns ``True`` (token consumed)
    or ``False`` (dropped).  When ``block=True`` and ``timeout`` is set,
    callers may skip the work rather than queue forever.
    """

    def __init__(
        self,
        *,
        overrides: dict[Lane, LaneConfig] | None = None,
        configs: dict[Lane, LaneConfig] | None = None,
    ) -> None:
        cfg = dict(DEFAULT_LANE_CONFIGS)
        if configs:
            cfg.update(configs)
        if overrides:
            cfg.update(overrides)
        self._buckets: dict[Lane, TokenBucket] = {
            lane: TokenBucket(capacity=c.capacity, refill_rate=c.refill_rate)
            for lane, c in cfg.items()
        }
        # Trade-write never has to wait behind data reads or warmup; this
        # invariant is enforced by *separate* buckets, not a global lock.

    def acquire(
        self,
        lane: Lane,
        *,
        n: float = 1.0,
        timeout: float | None = None,
    ) -> bool:
        if lane not in self._buckets:
            raise KeyError(f"unknown lane: {lane!r}")
        return self._buckets[lane].acquire(n=n, timeout=timeout)

    def try_acquire(self, lane: Lane, n: float = 1.0) -> bool:
        return self._buckets[lane].try_acquire(n)

    def stats(self) -> list[LaneStats]:
        out: list[LaneStats] = []
        for lane, b in self._buckets.items():
            with b._lock:
                b._refill_locked()
                out.append(
                    LaneStats(
                        lane=lane,
                        tokens=round(b._tokens, 3),
                        capacity=b.capacity,
                        total_acquired=b.total_acquired,
                        total_wait_seconds=round(b.total_wait_seconds, 3),
                    )
                )
        return out

    @contextmanager
    def slot(
        self,
        lane: Lane,
        *,
        timeout: float | None = 2.0,
    ) -> Iterator[bool]:
        """Context manager that yields ``True`` if a slot was acquired,
        ``False`` if dropped.  Useful as ``with limiter.slot(Lane.DATA_READ) as ok:``."""
        ok = self.acquire(lane, timeout=timeout)
        try:
            yield ok
        finally:
            pass


# ============================================================================
# Process-wide singleton accessor
# ============================================================================

_default_lock = threading.Lock()
_default: LaneLimiter | None = None


def get_limiter() -> LaneLimiter:
    """Return the process-wide ``LaneLimiter``.

    The default is lazy so module import is side-effect free.  Tests
    should pass their own ``LaneLimiter`` instance to the client instead
    of monkey-patching this global.
    """
    global _default
    with _default_lock:
        if _default is None:
            _default = LaneLimiter()
        return _default


def reset_default_limiter() -> None:
    """Drop the cached default limiter.  Intended for tests."""
    global _default
    with _default_lock:
        _default = None


__all__ = [
    "Lane",
    "LaneConfig",
    "LaneLimiter",
    "LaneStats",
    "TokenBucket",
    "DEFAULT_LANE_CONFIGS",
    "get_limiter",
    "reset_default_limiter",
]