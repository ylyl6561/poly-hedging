"""
Market module - Market discovery and RTDS price collection.

Exports:
- discover_fast_market_markets, find_best_fast_market
- collect_rtds_prices, start_rtds_stream, check_tick_health
- format_dual_track_comparison
"""

from .market_discovery import (
    parse_resolves_at,
    parse_fast_market_end_time,
    discover_fast_market_markets,
    discover_via_gamma,
    discover_deterministic_slots,
    find_best_fast_market,
)

from .rtds_prices import (
    collect_rtds_prices,
    collect_rtds_prices_with_retry,
    start_rtds_stream,
    stop_rtds_stream,
    check_tick_health,
    force_stream_reconnect,
    format_dual_track_comparison,
    PriceTick,
)

__all__ = [
    "parse_resolves_at",
    "parse_fast_market_end_time",
    "discover_fast_market_markets",
    "discover_via_gamma",
    "discover_deterministic_slots",
    "find_best_fast_market",
    "collect_rtds_prices",
    "collect_rtds_prices_with_retry",
    "start_rtds_stream",
    "stop_rtds_stream",
    "check_tick_health",
    "force_stream_reconnect",
    "format_dual_track_comparison",
    "PriceTick",
]
