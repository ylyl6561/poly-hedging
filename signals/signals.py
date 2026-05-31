"""
CEX price momentum signals for fast market trading.

Fetches price data from Binance or Coinbase and computes momentum,
direction, and volume ratios. This is the "alpha" input to the strategy.
"""

from datetime import datetime, timezone, timedelta
from urllib.parse import quote

from core.constants import ASSET_SYMBOLS, COINBASE_PRODUCTS
from api import api_request as _api_request


# =============================================================================
# Binance
# =============================================================================

def get_binance_momentum(symbol="BTCUSDT", lookback_minutes=5):
    """Get price momentum from Binance public API.

    Returns: {momentum_pct, direction, price_now, price_then, avg_volume,
              latest_volume, volume_ratio, candles}
    """
    url = (
        f"https://api.binance.com/api/v3/klines"
        f"?symbol={symbol}&interval=1m&limit={lookback_minutes}"
    )
    result = _api_request(url)
    if not result or isinstance(result, dict):
        return None

    try:
        candles = result
        if len(candles) < 2:
            return None

        price_then = float(candles[0][1])
        price_now = float(candles[-1][4])
        momentum_pct = ((price_now - price_then) / price_then) * 100
        direction = "up" if momentum_pct > 0 else "down"

        volumes = [float(c[5]) for c in candles]
        avg_volume = sum(volumes) / len(volumes)
        latest_volume = volumes[-1]
        volume_ratio = latest_volume / avg_volume if avg_volume > 0 else 1.0

        return {
            "momentum_pct": momentum_pct,
            "direction": direction,
            "price_now": price_now,
            "price_then": price_then,
            "avg_volume": avg_volume,
            "latest_volume": latest_volume,
            "volume_ratio": volume_ratio,
            "candles": len(candles),
        }
    except (IndexError, ValueError, KeyError):
        return None


def get_binance_open_price_at(symbol, start_ms):
    """Get the open price of the 1-minute candle starting at start_ms (unix ms).
    Used as the fixed window-open reference price for the N(d) fair-value model.
    """
    url = (
        f"https://api.binance.com/api/v3/klines"
        f"?symbol={symbol}&interval=1m&startTime={start_ms}&limit=1"
    )
    result = _api_request(url)
    if isinstance(result, list) and len(result) > 0:
        candle = result[0]
        if int(candle[0]) != int(start_ms):
            return None
        return float(candle[1])
    return None


# =============================================================================
# Coinbase
# =============================================================================

def get_coinbase_momentum(product="BTC-USD", lookback_minutes=5):
    """Get price momentum from Coinbase Exchange public candles."""
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(minutes=max(lookback_minutes, 2))
    url = (
        f"https://api.exchange.coinbase.com/products/{product}/candles"
        f"?granularity=60&start={quote(start.isoformat())}&end={quote(end.isoformat())}"
    )
    result = _api_request(url)
    if not result or isinstance(result, dict):
        return None

    try:
        # Coinbase format: [time, low, high, open, close, volume], newest first.
        candles = sorted(result, key=lambda c: c[0])
        if len(candles) < 2:
            return None

        price_then = float(candles[0][3])
        price_now = float(candles[-1][4])
        momentum_pct = ((price_now - price_then) / price_then) * 100
        direction = "up" if momentum_pct > 0 else "down"

        volumes = [float(c[5]) for c in candles]
        avg_volume = sum(volumes) / len(volumes)
        latest_volume = volumes[-1]
        volume_ratio = latest_volume / avg_volume if avg_volume > 0 else 1.0

        return {
            "momentum_pct": momentum_pct,
            "direction": direction,
            "price_now": price_now,
            "price_then": price_then,
            "avg_volume": avg_volume,
            "latest_volume": latest_volume,
            "volume_ratio": volume_ratio,
            "candles": len(candles),
            "source": "coinbase",
        }
    except (IndexError, ValueError, KeyError, TypeError):
        return None


def get_coinbase_open_price_at(product, start_ms):
    """Get Coinbase 1-minute candle open at start_ms."""
    start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    end = start + timedelta(minutes=1)
    url = (
        f"https://api.exchange.coinbase.com/products/{product}/candles"
        f"?granularity=60&start={quote(start.isoformat())}&end={quote(end.isoformat())}"
    )
    result = _api_request(url)
    if not result or isinstance(result, dict):
        return None
    try:
        candles = sorted(result, key=lambda c: c[0])
        for candle in candles:
            if int(candle[0]) == int(start.timestamp()):
                return float(candle[3])
    except (IndexError, ValueError, TypeError):
        return None
    return None


# =============================================================================
# Unified Interface
# =============================================================================

def get_momentum(asset="BTC", source="binance", lookback=5):
    """Get price momentum from configured source."""
    if source == "binance":
        symbol = ASSET_SYMBOLS.get(asset, "BTCUSDT")
        momentum = get_binance_momentum(symbol, lookback)
        if momentum:
            momentum["source"] = "binance"
            return momentum
        print("  ⚠️  Binance signal unavailable; falling back to Coinbase")
        product = COINBASE_PRODUCTS.get(asset, "BTC-USD")
        return get_coinbase_momentum(product, lookback)
    elif source == "coinbase":
        product = COINBASE_PRODUCTS.get(asset, "BTC-USD")
        return get_coinbase_momentum(product, lookback)
    elif source == "coingecko":
        print("  ⚠️  CoinGecko free tier doesn't provide candle data — switch to binance")
        return None
    else:
        return None


def get_cex_open_price_at(asset, source, start_ms):
    """Get the market-window open price, falling back when the configured CEX is unavailable."""
    if source == "coinbase":
        return get_coinbase_open_price_at(COINBASE_PRODUCTS.get(asset, "BTC-USD"), start_ms)
    price = get_binance_open_price_at(ASSET_SYMBOLS.get(asset, "BTCUSDT"), start_ms)
    if price is not None:
        return price
    print("  ⚠️  Binance open price unavailable; falling back to Coinbase")
    return get_coinbase_open_price_at(COINBASE_PRODUCTS.get(asset, "BTC-USD"), start_ms)
