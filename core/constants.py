"""
Constants used across the fastloop trading system.
"""

# Asset → Binance symbol mapping
ASSET_SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
}

COINBASE_PRODUCTS = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "SOL": "SOL-USD",
}

# Asset → Gamma API search patterns
ASSET_PATTERNS = {
    "BTC": ["bitcoin up or down"],
    "ETH": ["ethereum up or down"],
    "SOL": ["solana up or down"],
}

COINGECKO_ASSETS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}

# Polymarket CLOB API
CLOB_API = "https://clob.polymarket.com"

# Trading constants
TRADE_SOURCE = "sdk:fastloop"
SKILL_SLUG = "polymarket-fast-loop"
SMART_SIZING_PCT = 0.05  # 5% of balance per trade
MIN_SHARES_PER_ORDER = 5  # Polymarket minimum
MAX_SPREAD_PCT = 0.10     # Skip if CLOB bid-ask spread exceeds this

# Seconds per year for Black-Scholes
SECONDS_PER_YEAR = 31_536_000

# Polymarket crypto fee formula constants (from docs.polymarket.com/trading/fees)
# fee = C × p × POLY_FEE_RATE × (p × (1-p))^POLY_FEE_EXPONENT
POLY_FEE_RATE = 0.25       # Crypto markets
POLY_FEE_EXPONENT = 2      # Crypto markets

# Window duration → seconds
WINDOW_SECONDS = {"5m": 300, "15m": 900, "1h": 3600}

# Default supported event window durations (in env-friendly order)
SUPPORTED_WINDOWS = ("5m", "15m", "1h")

# Per-window default tuning for the dual-wallet strategy.
#
# These values are the BASELINE used when no per-window override is present in
# the config / env.  Any per-window override (`<window>_entry_timeout_sec` etc.)
# takes precedence; otherwise the fallback for that field is the corresponding
# baseline.
#
# The general rule of thumb for each baseline:
# - entry_timeout_sec:   max wait for both sides to fill, capped at ~70% of the
#                        window minus the force-close window.  Larger windows
#                        can wait longer.
# - force_close_window_sec: distance to end_time at which we stop waiting for
#                        a normal fill and start force-closing.  Larger windows
#                        keep more room for the regular close path because the
#                        absolute amount of time is still substantial.
# - min_seconds_before_start: minimum lead time before start_time to allow
#                        placing initial orders.  Larger windows need more
#                        time to spread order placement across slots.
#                        Supports negative values: < 0 means "allow up to |N|
#                        seconds after start_time to still place orders"
#                        (e.g. -10 ⇒ still allowed in the first 10s after start).
# - poll_interval_sec:   how frequently the main loop ticks.  For 5m windows
#                        we need sub-second resolution, while 15m/1h can be
#                        far more relaxed.
WINDOW_BASELINE_CONFIG: dict[str, dict[str, float]] = {
    "5m": {
        "entry_timeout_sec": 120,
        "force_close_window_sec": 50,
        "min_seconds_before_start": 30,
        "poll_interval_sec": 0.1,
        "fixed_sell_price": 0.6,
        "fak_close_price": 0.99,
        "entry_shares": 10.0,
        "entry_up_price": 0.5,
        "entry_down_price": 0.5,
        "outcome_poll_timeout_sec": 900,
        "outcome_poll_interval_sec": 5,
        "settlement_poll_timeout_sec": 180,
        "settlement_poll_interval_sec": 20,
        "settlement_stable_rounds": 3,
        "max_consecutive_losses": 2,
    },
    "15m": {
        "entry_timeout_sec": 480,
        "force_close_window_sec": 120,
        "min_seconds_before_start": -120,
        "poll_interval_sec": 0.5,
        "fixed_sell_price": 0.6,
        "fak_close_price": 0.99,
        "entry_shares": 10.0,
        "entry_up_price": 0.5,
        "entry_down_price": 0.5,
        "outcome_poll_timeout_sec": 1200,
        "outcome_poll_interval_sec": 5,
        "settlement_poll_timeout_sec": 240,
        "settlement_poll_interval_sec": 30,
        "settlement_stable_rounds": 3,
        "max_consecutive_losses": 2,
    },
    "1h": {
        "entry_timeout_sec": 1800,
        "force_close_window_sec": 600,
        "min_seconds_before_start": -600,
        "poll_interval_sec": 1.0,
        "fixed_sell_price": 0.55,
        "fak_close_price": 0.99,
        "entry_shares": 10.0,
        "entry_up_price": 0.5,
        "entry_down_price": 0.5,
        "outcome_poll_timeout_sec": 1800,
        "outcome_poll_interval_sec": 10,
        "settlement_poll_timeout_sec": 360,
        "settlement_poll_interval_sec": 30,
        "settlement_stable_rounds": 3,
        "max_consecutive_losses": 3,
    },
}


def get_window_baseline(window: str, field: str):
    """Look up a baseline value for a given window + field.

    Returns ``None`` when either the window or the field is unknown.  Callers
    should fall back to their own default in that case.
    """
    if not window:
        return None
    window_key = window.lower()
    bucket = WINDOW_BASELINE_CONFIG.get(window_key)
    if not bucket:
        return None
    return bucket.get(field)


def list_supported_windows() -> tuple[str, ...]:
    """Return the canonical tuple of supported window labels."""
    return SUPPORTED_WINDOWS
