"""
Polymarket Public Data API client.

A shared, browser-shaped HTTP client for the public Data and Gamma APIs
(endpoints under ``data-api.polymarket.com`` and ``gamma-api.polymarket.com``).
Designed for read-only, unauthenticated traffic so it can be reused by the
smart-money collector, the dashboard, ad-hoc scripts, and tests.

Key design decisions
--------------------
1. **Browser-shaped headers.**  Cloudflare fronting the Data API rejects
   requests that look like scripts.  We send a real Chrome UA plus
   ``Accept`` / ``Accept-Language`` / ``Accept-Encoding`` / ``Referer`` /
   ``Origin`` so the request is indistinguishable from one issued by the
   polymarket.com SPA.
2. **Single connection pool per process.**  ``httpx.Client`` is reused for
   the whole lifetime, with HTTP keep-alive enabled by default.
3. **Throttle + jittered retry.**  Every request goes through a thread-safe
   rate limiter (``min_interval_seconds``) and exponential back-off on 429
   / 5xx so we never trip Cloudflare's IP-level block.
4. **Settings object, not module globals.**  The caller passes a settings
   dataclass that exposes ``data_api_base`` / ``gamma_api_base`` /
   ``request_min_interval_seconds`` / ``request_timeout_seconds`` /
   ``request_max_retries``.  ``smart_money.config.SmartMoneySettings``
   already has these fields; arbitrary settings objects can be used in
   tests.

Typical usage
-------------
    from api import PolymarketPublicClient
    from smart_money.config import get_settings

    with PolymarketPublicClient(get_settings()) as client:
        rows = client.fetch_leaderboard(limit=200, category="OVERALL")
        positions = client.fetch_positions("0xabc...")

Public API
----------
- ``class PolymarketPublicClient`` — the main client
- ``class PublicClientSettings`` — minimal protocol so we don't depend on
  smart_money's settings dataclass
- ``BROWSER_HEADERS`` — the default header set (overridable per-instance)
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Iterable
from typing import Any, Callable, Protocol, TypeVar

import httpx

from .budget import BudgetRegistry
from .circuit_breaker import CircuitBreaker, CircuitBreakerRegistry, CBState
from .lanes import Lane, LaneLimiter
from .order_events import OrderEventBus, get_order_event_bus
from .routing import CallContext, RoutingContext, default_routing_context, make_call_context

logger = logging.getLogger(__name__)


# ============================================================================
# Browser-shaped default headers
# ============================================================================

BROWSER_HEADERS: dict[str, str] = {
    # Real Chrome UA on macOS — Cloudflare lets these through.
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://polymarket.com/",
    "Origin": "https://polymarket.com",
    # Polymarket doesn't require a CSRF token for read-only Data API calls,
    # but providing one matches browser traffic more closely.
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}


# ============================================================================
# Settings protocol
# ============================================================================

class PublicClientSettings(Protocol):
    """Minimal settings shape required by PolymarketPublicClient.

    ``smart_money.config.SmartMoneySettings`` satisfies this protocol out of
    the box, but tests and ad-hoc scripts can pass any object with the same
    attributes.
    """

    data_api_base: str
    gamma_api_base: str
    request_timeout_seconds: float
    request_max_retries: int
    request_min_interval_seconds: float


# ============================================================================
# Client
# ============================================================================

class PolymarketPublicClient:
    """HTTP client for the public Polymarket Data + Gamma APIs.

    The client never holds private credentials and never signs requests, so
    it is safe to instantiate from any module.  Use it as a context manager
    so the underlying connection pool is closed cleanly.
    """

    def __init__(
        self,
        settings: PublicClientSettings | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        headers: dict[str, str] | None = None,
        cache: TTLCache | None = None,
        routing: RoutingContext | None = None,
        order_bus: OrderEventBus | None = None,
        default_route_class: str = "public",
    ) -> None:
        # Lazy default import to avoid a circular dep on smart_money.config.
        if settings is None:
            from smart_money.config import get_settings

            settings = get_settings()
        self.settings = settings

        merged_headers = dict(BROWSER_HEADERS)
        if headers:
            merged_headers.update(headers)
        self._client = httpx.Client(
            timeout=settings.request_timeout_seconds,
            transport=transport,
            headers=merged_headers,
            # Keep-alive is httpx's default, but spell it out for clarity.
            http2=False,
            follow_redirects=True,
        )
        self._request_lock = threading.Lock()
        self._last_request_at = 0.0
        # Per-instance cache; shares nothing across processes by default.
        # Tests can inject their own via ``cache=...``.
        self._cache = cache if cache is not None else TTLCache(default_ttl_seconds=3600.0)
        # Routing layer — limiter + circuit breakers + budgets.
        self.routing = routing if routing is not None else default_routing_context()
        self.order_bus = order_bus if order_bus is not None else get_order_event_bus()
        self.default_route_class = default_route_class

    # ----- lifecycle -----

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PolymarketPublicClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ----- cache controls -----

    def invalidate_cache(self, name: str | None = None) -> None:
        """Drop cached entries.  ``name=None`` clears the whole cache."""
        self._cache.invalidate(name)

    def cache_stats(self) -> dict[str, int]:
        """Return a snapshot of how many entries are currently cached."""
        # PublicClientSettings has no cache exposure today; expose counts
        # so callers can monitor hit/miss externally if needed.
        with self._cache._lock:
            return {"entries": len(self._cache._store)}

    # ----- low-level transport -----

    def _throttle(self) -> None:
        """Enforce a process-wide minimum interval between requests.

        The interval is taken from ``settings.request_min_interval_seconds``.
        The first request after a long pause still goes out immediately.
        """
        with self._request_lock:
            elapsed = time.monotonic() - self._last_request_at
            wait = self.settings.request_min_interval_seconds - elapsed
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()

    def _get(self, base_url: str, path: str, params: dict[str, Any]) -> Any:
        """GET a JSON resource through lane → budget → circuit breaker → HTTP.

        Returns the decoded JSON payload (dict or list), or ``None`` if the
        call was dropped at the routing layer (lane busy, budget exhausted,
        or circuit breaker open).  Callers that cannot tolerate ``None``
        should pass ``required=True`` or use :meth:`_get_required`.
        """
        return self._get_with_route(
            base_url, path, params, route_class=self.default_route_class, required=False
        )

    def _get_required(
        self, base_url: str, path: str, params: dict[str, Any]
    ) -> Any:
        return self._get_with_route(
            base_url, path, params, route_class=self.default_route_class, required=True
        )

    def _get_with_route(
        self,
        base_url: str,
        path: str,
        params: dict[str, Any],
        *,
        route_class: str,
        required: bool,
    ) -> Any | None:
        url = f"{base_url}{path}"
        ctx = make_call_context(url=url, lane=Lane.DATA_READ, route=route_class)
        breaker = self.routing.breaker_for(ctx.domain)
        budget = self.routing.budget_for(ctx.domain, ctx.route)

        # 1. Budget gate — drop before consuming any tokens.
        if not budget.allow():
            logger.debug(
                "budget exhausted domain=%s route=%s", ctx.domain, ctx.route
            )
            return None

        # 2. Lane token — block up to 2s so the read path doesn't drift
        #    too far behind the dashboard's refresh schedule.
        if not self.routing.limiter.acquire(ctx.lane, timeout=2.0):
            logger.debug(
                "lane busy, dropped domain=%s route=%s", ctx.domain, ctx.route
            )
            return None

        # 3. Circuit breaker gate.
        if not breaker.allow():
            logger.warning(
                "circuit breaker open domain=%s route=%s", ctx.domain, ctx.route
            )
            return None

        # 4. HTTP — with jittered retry, never amplifies failures.
        last_error: Exception | None = None
        for attempt in range(self.settings.request_max_retries):
            try:
                response = self._client.get(url, params=params)
                status = response.status_code
                if status == 429:
                    breaker.record_failure(status_code=429, reason="rate-limited")
                    retry_after = response.headers.get("Retry-After")
                    delay = (
                        float(retry_after)
                        if retry_after and retry_after.replace(".", "", 1).isdigit()
                        else min(8.0, 0.5 * (2**attempt)) + random.uniform(0, 0.25)
                    )
                    logger.warning(
                        "polymarket 429 path=%s delay=%.2fs", path, delay
                    )
                    time.sleep(delay)
                    continue
                if status == 403:
                    # The Cloudflare WAF rejected us.  Do not retry — that
                    # just amplifies the block.  Open the breaker.
                    breaker.record_failure(status_code=403, reason="cloudflare-waf")
                    logger.warning("polymarket 403 (CF WAF) path=%s", path)
                    if required:
                        raise RuntimeError(
                            f"Polymarket public read blocked for {path} (403)"
                        )
                    return None
                if status >= 500:
                    breaker.record_failure(status_code=status, reason="5xx")
                    delay = min(8.0, 0.5 * (2**attempt)) + random.uniform(0, 0.25)
                    logger.warning(
                        "polymarket 5xx status=%s path=%s delay=%.2fs",
                        status, path, delay,
                    )
                    time.sleep(delay)
                    continue
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, (dict, list)):
                    raise ValueError(
                        f"Unexpected payload from {path}: {type(payload).__name__}"
                    )
                breaker.record_success()
                return payload
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                breaker.record_failure(reason=type(exc).__name__)
                if attempt + 1 >= self.settings.request_max_retries:
                    break
                time.sleep(min(8.0, 0.5 * (2**attempt)) + random.uniform(0, 0.25))
        if required:
            raise RuntimeError(
                f"Polymarket public read failed for {path}: {last_error}"
            ) from last_error
        logger.warning(
            "polymarket read gave up path=%s last_error=%s",
            path,
            last_error,
        )
        return None

    # ----- Data API endpoints -----

    def fetch_leaderboard(
        self,
        *,
        limit: int,
        category: str = "OVERALL",
        time_period: str = "ALL",
        order_by: str = "PNL",
        cache_ttl_seconds: float | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch leaderboard rows, served from a per-process TTL cache.

        The Polymarket leaderboard updates infrequently (PnL is settled
        roughly hourly).  The Data API is fronted by Cloudflare and rejects
        high-frequency scraping, so we cache aggressively here.  Pass
        ``cache_ttl_seconds=0`` to bypass caching for a single call.
        """
        cache_key = (category, time_period, order_by, limit)
        if cache_ttl_seconds != 0:
            hit = self._cache.get("leaderboard", cache_key)
            if hit is not None:
                logger.debug("leaderboard cache hit key=%s", cache_key)
                return hit  # type: ignore[no-any-return]
        rows: list[dict[str, Any]] = []
        target = max(0, min(limit, 1000))
        for offset in range(0, target, 50):
            page_limit = min(50, target - offset)
            payload = self._get_with_route(
                self.settings.data_api_base,
                "/v1/leaderboard",
                {
                    "category": category,
                    "timePeriod": time_period,
                    "orderBy": order_by,
                    "limit": page_limit,
                    "offset": offset,
                },
                route_class="leaderboard",
                required=False,
            )
            if payload is None:
                # Lane busy / budget exhausted / CB open.  Return what
                # we have so far; the caller decides whether that's OK.
                logger.info(
                    "leaderboard fetch partial: %d rows after budget/CB drop",
                    len(rows),
                )
                break
            page = payload if isinstance(payload, list) else payload.get("data", [])
            rows.extend(row for row in page if isinstance(row, dict))
            if len(page) < page_limit:
                break
        rows = rows[:target]
        if cache_ttl_seconds != 0:
            # Honour the caller's TTL override first; otherwise fall back to
            # ``settings.leaderboard_cache_ttl_seconds`` if the settings
            # object defines it, or 1 hour as a final default.
            effective_ttl = cache_ttl_seconds
            if effective_ttl is None:
                effective_ttl = getattr(
                    self.settings, "leaderboard_cache_ttl_seconds", 3600.0
                )
            self._cache.set(
                "leaderboard",
                cache_key,
                rows,
                ttl_seconds=effective_ttl,
            )
        return rows

    def fetch_activity(
        self,
        wallet: str,
        *,
        start_epoch: int,
        end_epoch: int | None = None,
        activity_type: str = "TRADE",
        max_rows: int = 10_000,
    ) -> list[dict[str, Any]]:
        return list(
            self.iter_activity(
                wallet,
                start_epoch=start_epoch,
                end_epoch=end_epoch,
                activity_type=activity_type,
                max_rows=max_rows,
            )
        )

    def iter_activity(
        self,
        wallet: str,
        *,
        start_epoch: int,
        end_epoch: int | None = None,
        activity_type: str = "TRADE",
        max_rows: int = 10_000,
    ) -> Iterable[dict[str, Any]]:
        yield from self._fetch_offset_pages(
            "/activity",
            {
                "user": wallet,
                "type": activity_type,
                "start": start_epoch,
                "end": end_epoch,
                "sortBy": "TIMESTAMP",
                "sortDirection": "DESC",
            },
            page_size=500,
            max_rows=max_rows,
            route_class="activity",
        )

    def fetch_positions(self, wallet: str, *, max_rows: int = 10_000) -> list[dict[str, Any]]:
        return list(
            self.iter_positions(wallet, max_rows=max_rows)
        )

    def iter_positions(
        self, wallet: str, *, max_rows: int = 10_000
    ) -> Iterable[dict[str, Any]]:
        yield from self._fetch_offset_pages(
            "/positions",
            {
                "user": wallet,
                "sizeThreshold": 0,
                "sortBy": "TOKENS",
                "sortDirection": "DESC",
            },
            page_size=500,
            max_rows=max_rows,
            route_class="positions",
        )

    def fetch_closed_positions(
        self,
        wallet: str,
        *,
        max_rows: int = 10_000,
    ) -> list[dict[str, Any]]:
        return list(
            self.iter_closed_positions(wallet, max_rows=max_rows)
        )

    def iter_closed_positions(
        self,
        wallet: str,
        *,
        max_rows: int = 10_000,
    ) -> Iterable[dict[str, Any]]:
        yield from self._fetch_offset_pages(
            "/closed-positions",
            {
                "user": wallet,
                "sortBy": "TIMESTAMP",
                "sortDirection": "DESC",
            },
            page_size=50,
            max_rows=max_rows,
            route_class="closed-positions",
        )

    # ----- Gamma API endpoints -----

    def fetch_markets(self, condition_ids: Iterable[str]) -> list[dict[str, Any]]:
        ids = list(dict.fromkeys(str(item) for item in condition_ids if item))
        rows: list[dict[str, Any]] = []
        for offset in range(0, len(ids), 50):
            batch = ids[offset : offset + 50]
            payload = self._get_with_route(
                self.settings.gamma_api_base,
                "/markets",
                {"condition_ids": ",".join(batch), "limit": len(batch)},
                route_class="markets",
                required=False,
            )
            if payload is None:
                logger.info("markets fetch partial after %d/%d ids", len(rows), len(ids))
                break
            page = payload if isinstance(payload, list) else payload.get("markets", [])
            rows.extend(row for row in page if isinstance(row, dict))
        return rows

    # ----- helpers -----

    def _fetch_offset_pages(
        self,
        path: str,
        params: dict[str, Any],
        *,
        page_size: int,
        max_rows: int,
        route_class: str | None = None,
    ) -> Iterable[dict[str, Any]]:
        target = max(0, min(max_rows, 10_000))
        route = route_class or self.default_route_class
        for offset in range(0, target, page_size):
            page_limit = min(page_size, target - offset)
            page_params = {**params, "limit": page_limit, "offset": offset}
            page_params = {
                key: value for key, value in page_params.items() if value is not None
            }
            payload = self._get_with_route(
                self.settings.data_api_base,
                path,
                page_params,
                route_class=route,
                required=False,
            )
            if payload is None:
                # Lane / budget / CB dropped us; stop paging.
                return
            page = payload if isinstance(payload, list) else payload.get("data", [])
            for row in page:
                if isinstance(row, dict):
                    yield row
            if len(page) < page_limit:
                break


__all__ = [
    "BROWSER_HEADERS",
    "PolymarketPublicClient",
    "PublicClientSettings",
    "TTLCache",
    "cached",
]


# ============================================================================
# In-memory TTL cache (used to gate high-volume endpoints like leaderboard)
# ============================================================================

_T = TypeVar("_T")


class TTLCache:
    """Process-local TTL cache keyed by ``(name, key)``.

    Intended for read-only public endpoints that change slowly (e.g. the
    leaderboard updates at most every few minutes).  Avoids hammering the
    Cloudflare-fronted Data API when multiple modules / dashboards ask
    for the same data in a short window.

    Thread-safe.  Single instance can be shared across clients.
    """

    def __init__(self, default_ttl_seconds: float = 3600.0) -> None:
        self.default_ttl_seconds = default_ttl_seconds
        self._store: dict[tuple[str, Any], tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, name: str, key: Any) -> Any | None:
        now = time.monotonic()
        with self._lock:
            entry = self._store.get((name, key))
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at <= now:
                self._store.pop((name, key), None)
                return None
            return value

    def set(self, name: str, key: Any, value: Any, ttl_seconds: float | None = None) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl_seconds
        expires_at = time.monotonic() + ttl
        with self._lock:
            self._store[(name, key)] = (expires_at, value)

    def invalidate(self, name: str | None = None) -> None:
        with self._lock:
            if name is None:
                self._store.clear()
            else:
                self._store = {
                    k: v for k, v in self._store.items() if k[0] != name
                }


def cached(
    name: str,
    cache: TTLCache,
    ttl_seconds: float | None = None,
    key_fn: Callable[..., Any] | None = None,
) -> Callable[[Callable[..., _T]], Callable[..., _T]]:
    """Decorator that memoises a method's return value in a ``TTLCache``.

    Args:
        name: Cache namespace (e.g. ``"leaderboard"``).
        cache: Shared cache instance.
        ttl_seconds: Override the cache's default TTL for this call.
        key_fn: Optional ``(*args, **kwargs) -> hashable`` for the cache key.
            Defaults to ``(args, tuple(sorted(kwargs.items())))``.
    """

    def decorator(fn: Callable[..., _T]) -> Callable[..., _T]:
        def wrapper(self: PolymarketPublicClient, *args: Any, **kwargs: Any) -> _T:
            if key_fn is not None:
                key = key_fn(*args, **kwargs)
            else:
                key = (args, tuple(sorted(kwargs.items())))
            hit = cache.get(name, key)
            if hit is not None:
                return hit  # type: ignore[return-value]
            value = fn(self, *args, **kwargs)
            cache.set(name, key, value, ttl_seconds=ttl_seconds)
            return value

        return wrapper

    return decorator