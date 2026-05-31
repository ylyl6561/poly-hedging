"""
Core module - Constants and configuration.

Exports:
- Constants from constants.py
- Config functions and schema from config.py
- Runtime config variables (resolved at import time)
"""

from .constants import (
    ASSET_SYMBOLS,
    COINBASE_PRODUCTS,
    ASSET_PATTERNS,
    COINGECKO_ASSETS,
    CLOB_API,
    TRADE_SOURCE,
    SKILL_SLUG,
    SMART_SIZING_PCT,
    MIN_SHARES_PER_ORDER,
    MAX_SPREAD_PCT,
    SECONDS_PER_YEAR,
    POLY_FEE_RATE,
    POLY_FEE_EXPONENT,
    WINDOW_SECONDS,
)

from .config import (
    CONFIG_SCHEMA,
    WALLET_LINK_RETRIES,
    WALLET_LINK_RETRY_DELAY,
    DIRECT_POLYMARKET_CLOB,
    DIRECT_CLOB_HOST,
    DIRECT_CLOB_CHAIN_ID,
    DIRECT_CLOB_SIGNATURE_TYPE,
    DIRECT_CLOB_FUNDER,
    load_config,
    get_config_path,
    update_config,
    resolve_config,
    load_env_file,
)

# Import runtime-resolved config variables (populated by resolve_config at module load)
# Note: historical variables are preserved here for backward compatibility
# with signals/, strategy/, state/ modules. The schema is cleaned in config.py.
from .config import (
    STRATEGY_MODE,
    EXECUTION_ROUTE,
    ORDER_TYPE,
    PREOPEN_POLL_INTERVAL_SEC,
    PREOPEN_LEAD_TIME_SEC,
    PREOPEN_GC_GRACE_SEC,
    PREOPEN_YES_SHARES_X,
    PREOPEN_YES_MAX_PRICE,
    PREOPEN_HEDGE_RATIO,
    PREOPEN_DOWN_RESTING_PRICE,
    PREOPEN_DOWN_SWITCH_TTL_SEC,
    PREOPEN_DOWN_FAK_MAX_PRICE,
    PREOPEN_MIN_ARB_EDGE,
    PREOPEN_MAX_ACTIONS_PER_EVENT,
    CANDIDATE_JOURNAL,
    CANDIDATE_JOURNAL_FILE,
)

__all__ = [
    # Constants
    "ASSET_SYMBOLS",
    "COINBASE_PRODUCTS",
    "ASSET_PATTERNS",
    "COINGECKO_ASSETS",
    "CLOB_API",
    "TRADE_SOURCE",
    "SKILL_SLUG",
    "SMART_SIZING_PCT",
    "MIN_SHARES_PER_ORDER",
    "MAX_SPREAD_PCT",
    "SECONDS_PER_YEAR",
    "POLY_FEE_RATE",
    "POLY_FEE_EXPONENT",
    "WINDOW_SECONDS",
    # Config functions
    "CONFIG_SCHEMA",
    "WALLET_LINK_RETRIES",
    "WALLET_LINK_RETRY_DELAY",
    "DIRECT_POLYMARKET_CLOB",
    "DIRECT_CLOB_HOST",
    "DIRECT_CLOB_CHAIN_ID",
    "DIRECT_CLOB_SIGNATURE_TYPE",
    "DIRECT_CLOB_FUNDER",
    "load_config",
    "get_config_path",
    "update_config",
    "resolve_config",
    "load_env_file",
    # Runtime resolved vars
    "STRATEGY_MODE",
    "EXECUTION_ROUTE",
    "ORDER_TYPE",
    "PREOPEN_POLL_INTERVAL_SEC",
    "PREOPEN_LEAD_TIME_SEC",
    "PREOPEN_GC_GRACE_SEC",
    "PREOPEN_YES_SHARES_X",
    "PREOPEN_YES_MAX_PRICE",
    "PREOPEN_HEDGE_RATIO",
    "PREOPEN_DOWN_RESTING_PRICE",
    "PREOPEN_DOWN_SWITCH_TTL_SEC",
    "PREOPEN_DOWN_FAK_MAX_PRICE",
    "PREOPEN_MIN_ARB_EDGE",
    "PREOPEN_MAX_ACTIONS_PER_EVENT",
    "CANDIDATE_JOURNAL",
    "CANDIDATE_JOURNAL_FILE",
]
