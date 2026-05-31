"""
API module - HTTP client utilities and Polymarket CLOB API.

Exports:
- api_request: generic HTTP GET/POST
- get_client: lazy Simmer SDK client
- get_direct_clob_client: Polymarket CLOB client
- fetch_market_outcome, fetch_live_prices, fetch_orderbook_summary
- direct_polymarket_trade, ensure_wallet_linked_with_retry
"""

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
    get_wallet_private_key,
    direct_polymarket_trade,
    ensure_wallet_linked_with_retry,
    reset_direct_clob_client,
    cancel_order,
    _extract_fill_price,
    _is_retryable_wallet_link_error,
    GAMMA_API,
    CLOB_API,
)

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
    "get_wallet_private_key",
    "direct_polymarket_trade",
    "ensure_wallet_linked_with_retry",
    "reset_direct_clob_client",
    "cancel_order",
    "_extract_fill_price",
    "_is_retryable_wallet_link_error",
    "GAMMA_API",
    "CLOB_API",
]
