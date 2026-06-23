"""
State module - State management and structured logging.

Exports:
- Daily spend tracking: load_daily_spend, save_daily_spend
- Market tracking: load_entered_markets, mark_market_entered, has_entered_market
- Quality state: load_market_quality_state, update_market_quality_state
- Oracle prices: get_oracle_open_price, save_oracle_open_price, recover_oracle_open_price
- Oracle samples: append_oracle_price_sample, append_oracle_price_samples
- Candidate journal: append_candidate_record
- Structured logging: StructuredRunLog
- Trade state: TradeStateManager, TradePhase, TradeRecord, AsyncOutcomePoller
"""

from .state import (
    load_daily_spend,
    save_daily_spend,
    load_entered_markets,
    save_entered_markets,
    mark_market_entered,
    has_entered_market,
    load_market_quality_state,
    save_market_quality_state,
    update_market_quality_state,
    append_candidate_record,
    load_oracle_open_prices,
    save_oracle_open_prices,
    get_oracle_open_price,
    save_oracle_open_price,
    load_oracle_price_samples,
    save_oracle_price_samples,
    append_oracle_price_sample,
    append_oracle_price_samples,
    recover_oracle_open_price,
)

from .structured_log import (
    StructuredRunLog,
    utc_now_iso,
)

from .trade_state import (
    TradeStateManager,
    TradePhase,
    TradeRecord,
    AsyncOutcomePoller,
    get_trade_state_manager,
    get_async_outcome_poller,
    init_trade_state,
)

__all__ = [
    # Daily spend
    "load_daily_spend",
    "save_daily_spend",
    # Entered markets
    "load_entered_markets",
    "save_entered_markets",
    "mark_market_entered",
    "has_entered_market",
    # Market quality
    "load_market_quality_state",
    "save_market_quality_state",
    "update_market_quality_state",
    # Candidate journal
    "append_candidate_record",
    # Oracle prices
    "load_oracle_open_prices",
    "save_oracle_open_prices",
    "get_oracle_open_price",
    "save_oracle_open_price",
    "load_oracle_price_samples",
    "save_oracle_price_samples",
    "append_oracle_price_sample",
    "append_oracle_price_samples",
    "recover_oracle_open_price",
    # Structured logging
    "StructuredRunLog",
    "utc_now_iso",
    # Trade state
    "TradeStateManager",
    "TradePhase",
    "TradeRecord",
    "AsyncOutcomePoller",
    "get_trade_state_manager",
    "get_async_outcome_poller",
    "init_trade_state",
]
