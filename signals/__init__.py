"""
Signals module - CEX price momentum signals and fair value model.

Exports:
- get_momentum, get_cex_open_price_at
- compute_fair_value, norm_cdf
- Filter classes: TrendConfirmFilter, ChopFilter, WindowPathScorer, AIJudgement
"""

from .signals import (
    get_momentum,
    get_cex_open_price_at,
    get_binance_momentum,
    get_binance_open_price_at,
    get_coinbase_momentum,
    get_coinbase_open_price_at,
)

from .fair_value import (
    compute_fair_value,
    norm_cdf,
)

from .filters import (
    open_sign,
    TrendConfirmFilter,
    TrendChaseFilter,
    ChopFilter,
    WindowPathScorer,
    AIJudgement,
    compute_dynamic_params,
)

__all__ = [
    # Signals
    "get_momentum",
    "get_cex_open_price_at",
    "get_binance_momentum",
    "get_binance_open_price_at",
    "get_coinbase_momentum",
    "get_coinbase_open_price_at",
    # Fair value
    "compute_fair_value",
    "norm_cdf",
    # Filters
    "open_sign",
    "TrendConfirmFilter",
    "TrendChaseFilter",
    "ChopFilter",
    "WindowPathScorer",
    "AIJudgement",
    "compute_dynamic_params",
]
