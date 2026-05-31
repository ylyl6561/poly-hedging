"""
Trading module - Trade execution layer.

Exports:
- execute_trade, get_portfolio, get_positions
- calculate_position_size, cleanup_stale_gtc_orders
- evaluate_chainlink_settlement_no_signal
"""

from .trading import (
    import_fast_market,
    get_market_details,
    get_portfolio,
    get_positions,
    execute_trade,
    calculate_position_size,
    cleanup_stale_gtc_orders,
)

from .oracle_settlement_no import (
    evaluate_chainlink_settlement_no_signal,
)

__all__ = [
    "import_fast_market",
    "get_market_details",
    "get_portfolio",
    "get_positions",
    "execute_trade",
    "calculate_position_size",
    "cleanup_stale_gtc_orders",
    "evaluate_chainlink_settlement_no_signal",
]
