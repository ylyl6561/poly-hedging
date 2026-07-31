"""
Per-domain circuit breaker for Polymarket HTTP calls.

The breaker prevents the read pipeline from amplifying Cloudflare's IP
blocks: when a domain starts returning 403 / 503 / connection errors,
the breaker *opens* and short-circuits subsequent calls to a configured
fallback (``None``, a cached value, or a stale ``data``).  After a cool-down
period it lets one probe request through (``HALF_OPEN``); on success it
closes, on failure it opens again.

The breaker is intentionally state-only — it does not call any HTTP
client itself.  The ``PolymarketPublicClient`` integrates it by calling
``cb.allow()`` before each request and ``cb.record_success()`` /
``cb.record_failure()`` afterwards.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

logger = logging.getLogger(__name__)


class CBState(str, Enum):
    CLOSED = "closed"        # normal traffic
    HALF_OPEN = "half_open"  # probing after cool-down
    OPEN = "open"            # failing fast


# Status codes we treat as "Cloudflare block / upstream failure" — these
# contribute to opening the breaker.  4xx like 404 are NOT counted as
# failures because they signal a correct request with a missing resource.
DEFAULT_FAILURE_STATUSES: frozenset[int] = frozenset({403, 408, 425, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class CircuitBreakerConfig:
    failure_threshold: int = 5        # consecutive failures before OPEN
    open_duration_seconds: float = 30.0
    half_open_max_probes: int = 1
    failure_statuses: frozenset[int] = DEFAULT_FAILURE_STATUSES


class CircuitBreaker:
    """Thread-safe per-domain circuit breaker.

    The breaker uses a *consecutive failures* counter rather than a
    sliding window — simpler, easier to reason about, and matches the
    Cloudflare failure pattern (a brief burst of 403s followed by a
    successful challenge).

    A single breaker should be shared across all requests to the same
    domain.  Multiple breakers are kept in a registry keyed by domain.
    """

    def __init__(self, name: str, config: CircuitBreakerConfig | None = None) -> None:
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self._state = CBState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._half_open_probes_in_flight = 0
        self._lock = threading.Lock()
        # Stats for observability
        self.transitions: list[tuple[float, CBState, str]] = []  # (ts, new_state, reason)
        self.total_opens = 0
        self.total_short_circuits = 0

    # ---- query ----

    @property
    def state(self) -> CBState:
        with self._lock:
            return self._maybe_transition_locked()

    def stats(self) -> dict[str, object]:
        with self._lock:
            self._maybe_transition_locked()
            return {
                "name": self.name,
                "state": self._state.value,
                "consecutive_failures": self._consecutive_failures,
                "total_opens": self.total_opens,
                "total_short_circuits": self.total_short_circuits,
            }

    # ---- gate ----

    def allow(self) -> bool:
        """Return ``True`` if the next call should be issued."""
        with self._lock:
            state = self._maybe_transition_locked()
            if state == CBState.CLOSED:
                return True
            if state == CBState.HALF_OPEN:
                if self._half_open_probes_in_flight < self.config.half_open_max_probes:
                    self._half_open_probes_in_flight += 1
                    return True
                return False
            # OPEN
            self.total_short_circuits += 1
            return False

    def record_success(self) -> None:
        with self._lock:
            state = self._maybe_transition_locked()
            if state == CBState.HALF_OPEN:
                self._half_open_probes_in_flight = max(
                    0, self._half_open_probes_in_flight - 1
                )
                self._transition_locked(CBState.CLOSED, reason="probe-success")
                self._consecutive_failures = 0
            else:
                # CLOSED — reset failure streak.
                self._consecutive_failures = 0

    def record_failure(
        self,
        *,
        status_code: int | None = None,
        reason: str = "http",
    ) -> None:
        with self._lock:
            state = self._maybe_transition_locked()
            if state == CBState.HALF_OPEN:
                self._half_open_probes_in_flight = max(
                    0, self._half_open_probes_in_flight - 1
                )
                self._transition_locked(CBState.OPEN, reason=f"probe-fail:{reason}")
                self._opened_at = time.monotonic()
                self.total_opens += 1
                return
            # CLOSED — count toward threshold
            if status_code is None or status_code in self.config.failure_statuses:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self.config.failure_threshold:
                    self._transition_locked(
                        CBState.OPEN,
                        reason=f"threshold:{self._consecutive_failures}",
                    )
                    self._opened_at = time.monotonic()
                    self.total_opens += 1

    # ---- internals ----

    def _maybe_transition_locked(self) -> CBState:
        if self._state == CBState.OPEN and self._opened_at is not None:
            elapsed = time.monotonic() - self._opened_at
            if elapsed >= self.config.open_duration_seconds:
                self._transition_locked(CBState.HALF_OPEN, reason="cool-down")
        return self._state

    def _transition_locked(self, new_state: CBState, *, reason: str) -> None:
        if new_state == self._state:
            return
        prev = self._state
        self._state = new_state
        self.transitions.append((time.monotonic(), new_state, reason))
        # Cap the audit log so memory can't grow unbounded.
        if len(self.transitions) > 200:
            del self.transitions[: len(self.transitions) - 200]
        if new_state == CBState.OPEN:
            logger.warning(
                "circuit breaker OPEN domain=%s reason=%s (prev=%s)",
                self.name, reason, prev.value,
            )
        elif new_state == CBState.HALF_OPEN:
            logger.info(
                "circuit breaker HALF_OPEN domain=%s reason=%s", self.name, reason
            )
        else:
            logger.info(
                "circuit breaker CLOSED domain=%s reason=%s", self.name, reason
            )


# ============================================================================
# Registry — one breaker per domain
# ============================================================================


class CircuitBreakerRegistry:
    """Thread-safe registry mapping domain -> :class:`CircuitBreaker`."""

    def __init__(
        self,
        *,
        default_config: CircuitBreakerConfig | None = None,
        config_for: Callable[[str], CircuitBreakerConfig] | None = None,
    ) -> None:
        self._default_config = default_config or CircuitBreakerConfig()
        self._config_for = config_for
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()

    def for_domain(self, domain: str) -> CircuitBreaker:
        with self._lock:
            cb = self._breakers.get(domain)
            if cb is None:
                cfg = (
                    self._config_for(domain)
                    if self._config_for is not None
                    else self._default_config
                )
                cb = CircuitBreaker(domain, config=cfg)
                self._breakers[domain] = cb
            return cb

    def all(self) -> list[CircuitBreaker]:
        with self._lock:
            return list(self._breakers.values())

    def stats(self) -> list[dict[str, object]]:
        return [cb.stats() for cb in self.all()]


_default_registry_lock = threading.Lock()
_default_registry: CircuitBreakerRegistry | None = None


def get_breaker_registry() -> CircuitBreakerRegistry:
    global _default_registry
    with _default_registry_lock:
        if _default_registry is None:
            _default_registry = CircuitBreakerRegistry()
        return _default_registry


def reset_default_breaker_registry() -> None:
    global _default_registry
    with _default_registry_lock:
        _default_registry = None


__all__ = [
    "CBState",
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitBreakerRegistry",
    "DEFAULT_FAILURE_STATUSES",
    "get_breaker_registry",
    "reset_default_breaker_registry",
]