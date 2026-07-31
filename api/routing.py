"""
Domain extraction and per-call routing metadata.

Each call through :class:`api.polymarket_public.PolymarketPublicClient`
computes a ``CallContext`` that captures the *logical* attributes
(domain, lane, route_class) that the lane / budget / circuit-breaker
layers need to make routing decisions.

Keeping this in its own module avoids circular imports between
``polymarket_public`` and ``lanes`` / ``circuit_breaker`` / ``budget``.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from .budget import BudgetRegistry, get_budget_registry
from .circuit_breaker import CircuitBreaker, CircuitBreakerRegistry, get_breaker_registry
from .lanes import Lane, LaneLimiter, get_limiter


@dataclass(frozen=True)
class CallContext:
    url: str
    domain: str
    path: str
    route: str            # coarse bucket used for budgets, e.g. "leaderboard"
    lane: Lane
    method: str = "GET"


def make_call_context(
    *,
    url: str,
    lane: Lane,
    route: str,
    method: str = "GET",
) -> CallContext:
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    path = parsed.path
    return CallContext(
        url=url,
        domain=domain,
        path=path,
        route=route,
        lane=lane,
        method=method,
    )


@dataclass
class RoutingContext:
    """Bundle of registries a client uses to make routing decisions.

    Tests / scripts can construct their own with non-default policies
    (e.g. tighter budgets) and inject into ``PolymarketPublicClient``.
    """

    limiter: LaneLimiter
    breakers: CircuitBreakerRegistry
    budgets: BudgetRegistry

    def breaker_for(self, domain: str) -> CircuitBreaker:
        return self.breakers.for_domain(domain)

    def budget_for(self, domain: str, route: str):
        return self.budgets.for_route(domain, route)


def default_routing_context() -> RoutingContext:
    return RoutingContext(
        limiter=get_limiter(),
        breakers=get_breaker_registry(),
        budgets=get_budget_registry(),
    )


__all__ = [
    "CallContext",
    "RoutingContext",
    "default_routing_context",
    "make_call_context",
]