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
