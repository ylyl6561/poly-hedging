"""
API module - HTTP client utilities and Polymarket CLOB API.

Exports:
- api_request: generic HTTP GET/POST
- get_client: lazy Simmer SDK client
- get_direct_clob_client: Polymarket CLOB client
- fetch_market_outcome, fetch_live_prices, fetch_orderbook_summary
- direct_polymarket_trade, ensure_wallet_linked_with_retry
"""

from .stderr_utils import install_sdk_logger_noise_filter

# 在最早时机拦截 py_clob_client_v2 SDK 的传输层瞬时噪音
# (e.g. "[py_clob_client_v2] request error: Server disconnected")。
# 这些是 SDK 在 httpx.RequestError 时调 logger.error 打出来的，
# 默认会直接写到真实 stderr，干扰运维日志。
install_sdk_logger_noise_filter()

from .api import (
    api_request,
    lookup_fee_rate,
    fetch_market_by_token,
    fetch_market_outcome,
    fetch_live_midpoint,
    fetch_live_prices,
    fetch_orderbook_summary,
    fetch_side_orderbook_price,
    get_client,
    get_direct_clob_client,
    should_use_direct_live_clob,
    get_execution_route,
    get_wallet_usdc_balance,
    fetch_token_balance,
    direct_polymarket_trade,
    ensure_wallet_linked_with_retry,
    reset_direct_clob_client,
    cancel_order,
    fetch_order_status,
    fetch_order_trades,
    _extract_fill_price,
    _is_retryable_wallet_link_error,
    GAMMA_API,
    CLOB_API,
)
from .order_events import (
    OrderEvent,
    OrderEventBus,
    OrderStatus,
    ATTENTION_STATUSES,
    get_order_event_bus,
    reset_default_order_event_bus,
    new_order_id,
)
try:
    # Phase 2: public read client (browser-shaped headers, token bucket,
    # circuit breaker, per-route budget).  Re-exported under the legacy
    # name for backwards compatibility with smart_money.client.
    from .polymarket_public import (  # noqa: F401
        PolymarketPublicClient,
        BROWSER_HEADERS,
        TTLCache,
        PublicClientSettings,
    )
except ImportError:  # pragma: no cover - optional / legacy module
    PolymarketPublicClient = None  # type: ignore[assignment]

__all__ = [
    "api_request",
    "lookup_fee_rate",
    "fetch_market_by_token",
    "fetch_market_outcome",
    "fetch_live_midpoint",
    "fetch_live_prices",
    "fetch_orderbook_summary",
    "fetch_side_orderbook_price",
    "get_client",
    "get_direct_clob_client",
    "should_use_direct_live_clob",
    "get_execution_route",
    "get_wallet_usdc_balance",
    "fetch_token_balance",
    "direct_polymarket_trade",
    "ensure_wallet_linked_with_retry",
    "reset_direct_clob_client",
    "cancel_order",
    "fetch_order_status",
    "fetch_order_trades",
    "_extract_fill_price",
    "_is_retryable_wallet_link_error",
    "GAMMA_API",
    "CLOB_API",
    "OrderEvent",
    "OrderEventBus",
    "OrderStatus",
    "ATTENTION_STATUSES",
    "get_order_event_bus",
    "reset_default_order_event_bus",
    "new_order_id",
    "PolymarketPublicClient",
    "BROWSER_HEADERS",
    "TTLCache",
    "PublicClientSettings",
]
