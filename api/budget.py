"""
Sliding-window request budgets.

A budget guards an *individual* (domain, route) pair against bursts.
Unlike the lane token-bucket (which serialises *all* traffic), budgets
are per-route so that "leaderboard refresh" cannot starve "activity fetch".

Design notes
------------
* Bucket-per-window: we keep ``n_windows`` short buckets of equal width.
  On each call we increment the current bucket and sum the window.
* The window is short (default 5 min) so Cloudflare's IP-level throttle
  does not engage; the budget is the *floor* below which we are
  guaranteed to be safe.
* Budget is enforced *before* the lane acquire; if a route is over
  budget, the call is skipped without consuming a token (saves tokens
  for the next legitimate window).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BudgetConfig:
    window_seconds: float = 300.0        # 5 minutes
    window_buckets: int = 6              # 6 × 50s buckets
    max_requests_per_window: int = 30    # ≤ 0.1 req/s sustained

    def __post_init__(self) -> None:
        if self.window_buckets < 2:
            raise ValueError("window_buckets must be >= 2")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        if self.max_requests_per_window < 1:
            raise ValueError("max_requests_per_window must be >= 1")


class SlidingBudget:
    """Thread-safe sliding-window counter."""

    def __init__(self, config: BudgetConfig | None = None) -> None:
        self.config = config or BudgetConfig()
        if self.config.window_buckets < 2:
            raise ValueError("window_buckets must be >= 2")
        self._bucket_width = self.config.window_seconds / self.config.window_buckets
        # bucket_end → count
        self._buckets: deque[tuple[float, int]] = deque()
        self._total = 0
        self._lock = threading.Lock()

    def _trim_locked(self, now: float) -> None:
        cutoff = now - self.config.window_seconds
        while self._buckets and self._buckets[0][0] <= cutoff:
            _, count = self._buckets.popleft()
            self._total -= count

    def allow(self, n: int = 1) -> bool:
        now = time.monotonic()
        with self._lock:
            self._trim_locked(now)
            if self._total + n > self.config.max_requests_per_window:
                return False
            # Add to current bucket.
            bucket_end = self._bucket_index_end(now)
            if self._buckets and self._buckets[-1][0] == bucket_end:
                last_end, last_count = self._buckets.pop()
                self._buckets.append((last_end, last_count + n))
            else:
                self._buckets.append((bucket_end, n))
            self._total += n
            return True

    def current_usage(self) -> int:
        now = time.monotonic()
        with self._lock:
            self._trim_locked(now)
            return self._total

    def _bucket_index_end(self, now: float) -> float:
        idx = int(now // self._bucket_width) + 1
        return idx * self._bucket_width

    def stats(self) -> dict[str, object]:
        with self._lock:
            return {
                "current": self._total,
                "max": self.config.max_requests_per_window,
                "window_seconds": self.config.window_seconds,
            }


# ============================================================================
# Per-(domain, route) budget registry
# ============================================================================


class BudgetRegistry:
    """Map ``(domain, route)`` → :class:`SlidingBudget`."""

    def __init__(
        self,
        *,
        default_config: BudgetConfig | None = None,
        config_for: Callable[[str, str], BudgetConfig] | None = None,
    ) -> None:
        self._default_config = default_config or BudgetConfig()
        self._config_for = config_for
        self._budgets: dict[tuple[str, str], SlidingBudget] = {}
        self._lock = threading.Lock()

    def for_route(self, domain: str, route: str) -> SlidingBudget:
        with self._lock:
            key = (domain, route)
            b = self._budgets.get(key)
            if b is None:
                cfg = (
                    self._config_for(domain, route)
                    if self._config_for is not None
                    else self._default_config
                )
                b = SlidingBudget(cfg)
                self._budgets[key] = b
            return b

    def all(self) -> list[SlidingBudget]:
        with self._lock:
            return list(self._budgets.values())

    def stats(self) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        with self._lock:
            items = list(self._budgets.items())
        for (domain, route), _ in items:
            b = self._budgets[(domain, route)]
            s = b.stats()
            s.update({"domain": domain, "route": route})
            out.append(s)
        return out


# Default per-domain budgets.  Keep these conservative; bump them only
# when we have telemetry that Cloudflare is *not* triggering.
DEFAULT_DOMAIN_BUDGETS: dict[str, int] = {
    # 30 req / 5 min = 0.1 req/s — far below the IP threshold we hit.
    "data-api.polymarket.com": 30,
    # 120 req / 5 min — gamma is publicly consumed by the SPA; looser cap.
    "gamma-api.polymarket.com": 120,
    # Reads against CLOB book/price don't go through Cloudflare (clob is
    # not CF-fronted as aggressively), but we still cap to avoid RPS
    # issues against the upstream service.
    "clob-api.polymarket.com": 60,
}


def default_config_for(domain: str, route: str) -> BudgetConfig:
    max_per_window = DEFAULT_DOMAIN_BUDGETS.get(domain, 60)
    return BudgetConfig(
        window_seconds=300.0,
        window_buckets=6,
        max_requests_per_window=max_per_window,
    )


_default_budget_registry_lock = threading.Lock()
_default_budget_registry: BudgetRegistry | None = None


def get_budget_registry() -> BudgetRegistry:
    global _default_budget_registry
    with _default_budget_registry_lock:
        if _default_budget_registry is None:
            _default_budget_registry = BudgetRegistry(config_for=default_config_for)
        return _default_budget_registry


def reset_default_budget_registry() -> None:
    global _default_budget_registry
    with _default_budget_registry_lock:
        _default_budget_registry = None


__all__ = [
    "BudgetConfig",
    "BudgetRegistry",
    "DEFAULT_DOMAIN_BUDGETS",
    "SlidingBudget",
    "default_config_for",
    "get_budget_registry",
    "reset_default_budget_registry",
]