"""
Low-level HTTP and API client utilities.

Exports:
- api_request(): generic HTTP GET/POST with error handling
- get_simmer_client(): lazy singleton Simmer SDK client
- get_direct_clob_client(): lazy Polymarket CLOB client for direct trading
- wallet_link utilities
"""

import os
import sys
import json
import time
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from urllib.parse import quote

from core.constants import CLOB_API, TRADE_SOURCE, SKILL_SLUG
from core import (
    DIRECT_CLOB_HOST, DIRECT_CLOB_CHAIN_ID, DIRECT_CLOB_SIGNATURE_TYPE,
    DIRECT_CLOB_FUNDER, WALLET_LINK_RETRIES, WALLET_LINK_RETRY_DELAY,
    ORDER_TYPE,
)


# =============================================================================
# HTTP Helpers
# =============================================================================

_client = None
_direct_clob_client = None


def reset_direct_clob_client():
    """Reset the cached ClobClient to force a fresh instance on next use.

    Use this only after auth or version mismatch errors. Recreating the client
    before every trade adds latency and repeatedly hits /auth/api-key.
    """
    global _direct_clob_client
    _direct_clob_client = None


def api_request(url, method="GET", data=None, headers=None, timeout=15):
    """Make an HTTP request. Returns parsed JSON or None on error."""
    try:
        req_headers = headers or {}
        if "User-Agent" not in req_headers:
            req_headers["User-Agent"] = "simmer-fastloop_market/1.0"
        body = None
        if data:
            body = json.dumps(data).encode("utf-8")
            req_headers["Content-Type"] = "application/json"
        req = Request(url, data=body, headers=req_headers, method=method)
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        try:
            error_body = json.loads(e.read().decode("utf-8"))
            return {"error": error_body.get("detail", str(e)), "status_code": e.code}
        except Exception:
            return {"error": str(e), "status_code": e.code}
    except URLError as e:
        return {"error": f"Connection error: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}


def lookup_fee_rate(token_id):
    """Fetch taker fee rate (bps) from Polymarket CLOB for a token. Returns 0 on failure."""
    result = api_request(f"{CLOB_API}/fee-rate?token_id={quote(str(token_id))}", timeout=5)
    if not result or not isinstance(result, dict) or result.get("error"):
        return 0
    try:
        return int(float(result.get("base_fee") or 0))
    except (ValueError, TypeError):
        return 0


def fetch_market_by_token(token_id):
    """Fetch the parent CLOB market for a token id using Polymarket's V2 endpoint."""
    if not token_id:
        return None
    result = api_request(f"{CLOB_API}/markets-by-token/{quote(str(token_id))}", timeout=5)
    if not result or not isinstance(result, dict) or result.get("error"):
        return None
    return result


GAMMA_API = "https://gamma-api.polymarket.com"


def _coerce_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "closed", "resolved"}
    return bool(value)


def _coerce_json_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _coerce_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_token_winner(market):
    tokens = market.get("tokens")
    if not isinstance(tokens, list):
        return None

    for token in tokens:
        if not isinstance(token, dict):
            continue
        if _coerce_bool(token.get("winner")):
            outcome = token.get("outcome")
            if outcome:
                return str(outcome)

    for token in tokens:
        if not isinstance(token, dict):
            continue
        price = _coerce_float(token.get("price"))
        outcome = token.get("outcome")
        if outcome and price is not None and price >= 0.999:
            return str(outcome)

    return None


def _extract_outcome_prices_winner(market):
    outcomes = _coerce_json_list(market.get("outcomes"))
    prices = _coerce_json_list(market.get("outcomePrices"))
    if len(outcomes) != len(prices):
        return None

    for outcome, price_value in zip(outcomes, prices):
        price = _coerce_float(price_value)
        if outcome and price is not None and price >= 0.999:
            return str(outcome)

    return None


def _extract_outcome_from_market(market, market_id=None):
    if not isinstance(market, dict):
        return None
    if market_id:
        market_id = str(market_id)
        identifiers = {
            str(market.get("conditionId") or ""),
            str(market.get("condition_id") or ""),
            str(market.get("slug") or ""),
            str(market.get("marketId") or ""),
            str(market.get("id") or ""),
        }
        if market_id not in identifiers:
            return None

    outcome = (
        market.get("outcome")
        or market.get("winningOutcome")
        or market.get("winning_outcome")
    )
    winner = market.get("winner") or market.get("winningOutcome") or market.get("winning_outcome")
    if not outcome and not winner:
        winner = _extract_token_winner(market) or _extract_outcome_prices_winner(market)
        outcome = winner
    settled = (
        _coerce_bool(market.get("resolved"))
        or _coerce_bool(market.get("settled"))
        or _coerce_bool(market.get("closed"))
        or _coerce_bool(market.get("active"), default=True) is False
        or bool(winner)
    )
    if outcome or winner or settled:
        return {
            "outcome": outcome,
            "winner": winner,
            "settled": settled,
        }
    return None


def _iter_gamma_markets(payload):
    if isinstance(payload, list):
        yield from payload
        return
    if not isinstance(payload, dict):
        return
    if isinstance(payload.get("markets"), list):
        yield from payload["markets"]
    elif isinstance(payload.get("data"), list):
        yield from payload["data"]
    elif payload.get("conditionId") or payload.get("slug"):
        yield payload


def fetch_market_outcome(market_id, slug=None, clob_token_ids=None):
    """Fetch settled outcome for a Polymarket market.

    Args:
        market_id: The market condition_id or slug.
        slug: Optional Gamma event/market slug for fallback lookup.
        clob_token_ids: Optional YES/NO token ids for token-to-market fallback.

    Returns:
        dict with keys: outcome (str: "YES" or "NO"), winner (str or None),
        settled (bool), or None on failure.
    """
    if not market_id and not slug and not clob_token_ids:
        return None

    # Try CLOB market info endpoint first (most reliable for settled markets)
    saw_unsettled = False
    if market_id:
        result = api_request(f"{CLOB_API}/markets/{quote(str(market_id))}", timeout=10)
        if result and isinstance(result, dict) and not result.get("error"):
            extracted = _extract_outcome_from_market(result)
            if extracted and (extracted.get("outcome") or extracted.get("winner")):
                return extracted
            if extracted and not extracted.get("settled"):
                saw_unsettled = True

    gamma_urls = []
    if slug:
        quoted_slug = quote(str(slug))
        gamma_urls.append(f"{GAMMA_API}/events/slug/{quoted_slug}")
        gamma_urls.append(f"{GAMMA_API}/markets/slug/{quoted_slug}")
    if market_id:
        quoted_market_id = quote(str(market_id))
        gamma_urls.append(f"{GAMMA_API}/markets?condition_ids={quoted_market_id}")
        gamma_urls.append(f"{GAMMA_API}/markets?conditionId={quoted_market_id}")
        gamma_urls.append(f"{GAMMA_API}/markets?market={quoted_market_id}")
    for token_id in clob_token_ids or []:
        gamma_urls.append(f"{CLOB_API}/markets-by-token/{quote(str(token_id))}")

    for url in gamma_urls:
        payload = api_request(url, timeout=10)
        if not payload or not isinstance(payload, (dict, list)) or (isinstance(payload, dict) and payload.get("error")):
            continue
        for market in _iter_gamma_markets(payload):
            extracted = _extract_outcome_from_market(market, market_id=market_id)
            if not extracted and slug:
                extracted = _extract_outcome_from_market(market, market_id=slug)
            if not extracted:
                continue
            if extracted.get("outcome") or extracted.get("winner"):
                extracted["source"] = "gamma" if "gamma-api" in url else "clob_token"
                return extracted
            if not extracted.get("settled"):
                saw_unsettled = True

    if saw_unsettled:
        return {
            "outcome": None,
            "winner": None,
            "settled": False,
            "source": "fallback",
        }

    return None


def fetch_live_midpoint(token_id):
    """Fetch live midpoint price from Polymarket CLOB for a single token."""
    result = api_request(f"{CLOB_API}/midpoint?token_id={quote(str(token_id))}", timeout=5)
    if not result or not isinstance(result, dict) or result.get("error"):
        return None
    try:
        return float(result["mid"])
    except (KeyError, ValueError, TypeError):
        return None


def fetch_live_prices(clob_token_ids):
    """Fetch live YES midpoint from Polymarket CLOB.

    Args:
        clob_token_ids: List of [yes_token_id, no_token_id] from Gamma.

    Returns:
        float or None: Live YES price (0-1).
    """
    if not clob_token_ids or len(clob_token_ids) < 1:
        return None
    return fetch_live_midpoint(clob_token_ids[0])


def fetch_orderbook_summary(clob_token_ids):
    """Fetch order book for YES token and return spread + depth summary.

    Args:
        clob_token_ids: List of [yes_token_id, no_token_id] from Gamma.

    Returns:
        dict with spread_pct, best_bid, best_ask, bid_depth_usd, ask_depth_usd
        or None on failure.
    """
    if not clob_token_ids or len(clob_token_ids) < 1:
        return None
    yes_token = clob_token_ids[0]
    result = api_request(f"{CLOB_API}/book?token_id={quote(str(yes_token))}", timeout=5)
    if not result or not isinstance(result, dict):
        return None

    bids = result.get("bids", [])
    asks = result.get("asks", [])
    if not bids or not asks:
        return None

    try:
        sorted_bids = sorted(bids, key=lambda x: float(x.get("price", 0)), reverse=True)
        sorted_asks = sorted(asks, key=lambda x: float(x.get("price", 0)))
        best_bid = float(sorted_bids[0]["price"])
        best_ask = float(sorted_asks[0]["price"])
        spread = best_ask - best_bid
        mid = (best_ask + best_bid) / 2
        spread_pct = spread / mid if mid > 0 else 0

        bid_depth = sum(float(b.get("size", 0)) * float(b.get("price", 0)) for b in sorted_bids[:5])
        ask_depth = sum(float(a.get("size", 0)) * float(a.get("price", 0)) for a in sorted_asks[:5])

        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread_pct": spread_pct,
            "bid_depth_usd": bid_depth,
            "ask_depth_usd": ask_depth,
        }
    except (KeyError, ValueError, IndexError, TypeError):
        return None


def fetch_side_orderbook_price(clob_token_ids, side):
    """Fetch executable top-of-book price for buying YES or NO.

    Returns the side token's best ask plus a small depth summary. This is safer
    than deriving NO from the YES midpoint in fast markets, where one side can
    be stale or very wide.
    """
    if not clob_token_ids or len(clob_token_ids) < 2:
        return None
    side = (side or "").lower()
    token_idx = 0 if side == "yes" else 1 if side == "no" else None
    if token_idx is None:
        return None

    token_id = clob_token_ids[token_idx]
    result = api_request(f"{CLOB_API}/book?token_id={quote(str(token_id))}", timeout=5)
    if not result or not isinstance(result, dict):
        return None

    bids = result.get("bids", [])
    asks = result.get("asks", [])
    if not asks:
        return None

    try:
        sorted_asks = sorted(asks, key=lambda x: float(x.get("price", 0)))
        sorted_bids = sorted(bids, key=lambda x: float(x.get("price", 0)), reverse=True) if bids else []
        best_ask = float(sorted_asks[0]["price"])
        best_bid = float(sorted_bids[0]["price"]) if sorted_bids else None
        ask_depth = sum(float(a.get("size", 0)) * float(a.get("price", 0)) for a in sorted_asks[:5])
        spread = (best_ask - best_bid) if best_bid is not None else None
        mid = ((best_ask + best_bid) / 2) if best_bid is not None else None
        spread_pct = (spread / mid) if mid and mid > 0 else None
        return {
            "side": side,
            "token_id": str(token_id),
            "best_ask": best_ask,
            "best_bid": best_bid,
            "ask_depth_usd": ask_depth,
            "spread": spread,
            "spread_pct": spread_pct,
        }
    except (KeyError, ValueError, IndexError, TypeError):
        return None


# =============================================================================
# Simmer Client
# =============================================================================

def get_client(live=True):
    """Lazy-init SimmerClient singleton."""
    global _client
    if _client is None:
        try:
            from runtime_clients.simmer_factory import create_simmer_client
        except ImportError:
            print("Error: simmer-sdk not installed. Run: pip install simmer-sdk")
            sys.exit(1)
        api_key = os.environ.get("SIMMER_API_KEY")
        if not api_key:
            print("Error: SIMMER_API_KEY environment variable not set")
            print("Get your API key from: simmer.markets/dashboard → SDK tab")
            sys.exit(1)
        venue = os.environ.get("TRADING_VENUE", "polymarket")
        _client = create_simmer_client(api_key=api_key, venue=venue, live=live)
    return _client


def should_use_direct_live_clob(dry_run):
    from core.config import EXECUTION_ROUTE
    return (
        not dry_run
        and EXECUTION_ROUTE == "direct_clob"
        and os.environ.get("TRADING_VENUE", "polymarket") == "polymarket"
        and bool(get_wallet_private_key())
    )


def get_execution_route():
    from core.config import EXECUTION_ROUTE
    return EXECUTION_ROUTE


def get_wallet_private_key(env_var_name: str | None = None):
    key_name = env_var_name or "WALLET_PRIVATE_KEY"
    return os.environ.get(key_name)


def get_wallet_address(env_var_name: str | None = None):
    private_key = get_wallet_private_key(env_var_name)
    if not private_key:
        return None
    try:
        from eth_account import Account
        return Account.from_key(private_key).address
    except Exception:
        return None


def _extract_usdc_balance(payload):
    if payload is None:
        return None
    if isinstance(payload, (int, float)):
        return float(payload)
    if isinstance(payload, str):
        try:
            return float(payload)
        except (TypeError, ValueError):
            return None
    if isinstance(payload, list):
        for item in payload:
            parsed = _extract_usdc_balance(item)
            if parsed is not None:
                return parsed
        return None
    if not isinstance(payload, dict):
        return None

    candidate_keys = (
        "balance_usdc", "balance", "available", "available_usdc", "usdc", "usdc_balance",
        "amount", "free", "freeCollateral", "free_collateral", "buyingPower", "buying_power",
        "total", "value",
    )
    for key in candidate_keys:
        value = payload.get(key)
        try:
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            continue

    for nested_key in ("balance", "allowance", "data"):
        nested = payload.get(nested_key)
        parsed = _extract_usdc_balance(nested)
        if parsed is not None:
            return parsed
    return None


def get_wallet_usdc_balance(*, env_var_name: str | None = None) -> dict:
    private_key = get_wallet_private_key(env_var_name)
    if not private_key:
        return {"success": False, "error": f"missing_private_key:{env_var_name or 'WALLET_PRIVATE_KEY'}"}

    wallet_address = get_wallet_address(env_var_name)
    if not wallet_address:
        return {"success": False, "error": "wallet_address_unavailable"}

    try:
        client = get_direct_clob_client(private_key=private_key)
    except Exception as exc:
        return {"success": False, "error": f"clob_client_init_failed:{exc}", "wallet_address": wallet_address}

    try:
        payload = client.get_balance_allowance()
    except TypeError:
        try:
            payload = client.get_balance_allowance(params={})
        except Exception as exc:
            payload = {"error": str(exc)}
    except Exception as exc:
        payload = {"error": str(exc)}

    parsed = _extract_usdc_balance(payload)
    if parsed is not None:
        return {"success": True, "wallet_address": wallet_address, "balance_usdc": parsed, "raw": payload}

    value_payload = api_request(f"https://data-api.polymarket.com/value?user={quote(str(wallet_address))}", timeout=10)
    parsed = _extract_usdc_balance(value_payload)
    if parsed is not None:
        return {"success": True, "wallet_address": wallet_address, "balance_usdc": parsed, "raw": value_payload}

    return {"success": False, "wallet_address": wallet_address, "error": "balance_unavailable"}


def get_wallet_address(env_var_name: str | None = None):
    private_key = get_wallet_private_key(env_var_name)
    if not private_key:
        return None
    try:
        from eth_account import Account
        return Account.from_key(private_key).address
    except Exception:
        return None


def get_wallet_usdc_balance(*, env_var_name: str | None = None) -> dict:
    private_key = get_wallet_private_key(env_var_name)
    if not private_key:
        return {"success": False, "error": f"missing_private_key:{env_var_name or 'WALLET_PRIVATE_KEY'}"}

    wallet_address = get_wallet_address(env_var_name)
    if not wallet_address:
        return {"success": False, "error": "wallet_address_unavailable"}

    try:
        client = get_direct_clob_client(private_key=private_key)
    except Exception as exc:
        return {"success": False, "error": f"clob_client_init_failed:{exc}", "wallet_address": wallet_address}

    balance_candidates = []
    try:
        balance_candidates.append(client.get_balance_allowance())
    except TypeError:
        try:
            balance_candidates.append(client.get_balance_allowance(params={}))
        except Exception as exc:
            balance_candidates.append({"error": str(exc)})
    except Exception as exc:
        balance_candidates.append({"error": str(exc)})

    for payload in balance_candidates:
        parsed = _extract_usdc_balance(payload)
        if parsed is not None:
            return {"success": True, "wallet_address": wallet_address, "balance_usdc": parsed, "raw": payload}

    value_payload = api_request(f"https://data-api.polymarket.com/value?user={quote(str(wallet_address))}", timeout=10)
    parsed_value = _extract_usdc_balance(value_payload)
    if parsed_value is not None:
        return {"success": True, "wallet_address": wallet_address, "balance_usdc": parsed_value, "raw": value_payload}

    positions_payload = api_request(f"https://data-api.polymarket.com/positions?user={quote(str(wallet_address))}", timeout=10)
    parsed_positions = _extract_usdc_balance(positions_payload)
    if parsed_positions is not None:
        return {"success": True, "wallet_address": wallet_address, "balance_usdc": parsed_positions, "raw": positions_payload}

    return {"success": False, "wallet_address": wallet_address, "error": "balance_unavailable"}


# =============================================================================
# Direct Polymarket CLOB Client
# =============================================================================

def get_direct_clob_client(*, private_key: str | None = None):
    """Create an authenticated Polymarket CLOB V2 client without Simmer wallet linking."""
    global _direct_clob_client
    if private_key is None and _direct_clob_client is not None:
        return _direct_clob_client

    private_key = private_key or get_wallet_private_key()
    if not private_key:
        raise RuntimeError("WALLET_PRIVATE_KEY is required for direct Polymarket CLOB live trading")

    try:
        from py_clob_client_v2 import ClobClient
    except ImportError as e:
        raise RuntimeError("py-clob-client-v2 is required for direct Polymarket CLOB trading") from e

    funder = DIRECT_CLOB_FUNDER
    if not funder:
        try:
            from eth_account import Account
            funder = Account.from_key(private_key).address
        except Exception:
            funder = None

    client = ClobClient(
        DIRECT_CLOB_HOST,
        key=private_key,
        chain_id=DIRECT_CLOB_CHAIN_ID,
        signature_type=DIRECT_CLOB_SIGNATURE_TYPE,
        funder=funder,
    )
    creds = client.create_or_derive_api_key()
    client.set_api_creds(creds)
    if private_key == get_wallet_private_key():
        _direct_clob_client = client
    return client


def _get_order_type_enum(override=None):
    try:
        from py_clob_client_v2 import OrderType
    except ImportError as e:
        raise RuntimeError("py-clob-client-v2 is required for direct Polymarket CLOB trading") from e

    if override:
        override = override.upper()
        mapping = {
            "GTC": OrderType.GTC,
            "GTD": OrderType.GTD,
            "FAK": OrderType.FAK,
            "FOK": OrderType.FOK,
        }
        return mapping.get(override, OrderType.GTC)

    order_type = (ORDER_TYPE or "GTC").upper()
    mapping = {
        "GTC": OrderType.GTC,
        "GTD": OrderType.GTD,
        "FAK": OrderType.FAK,
        "FOK": OrderType.FOK,
    }
    return mapping.get(order_type, OrderType.GTC)


def _extract_order_id(result):
    if not isinstance(result, dict):
        return None
    for key in ("orderID", "orderId", "order_id", "id"):
        if result.get(key):
            return result.get(key)
    if isinstance(result.get("order"), dict):
        return _extract_order_id(result["order"])
    return None


def _extract_fill_price(result):
    """
    Try to extract actual average fill price from the V2 CLOB order result.

    The result dict may contain:
      - result["price"]           — limit price of the order
      - result["fills"]          — list of individual fill objects
      - result["fills"][i]["price"]  — each fill's price
      - result["fills"][i]["p"]      — alternative fill price key
      - result["averagePrice"]    — average fill price
      - result["order"]["price"]  — nested order price
      - result["order"]["fills"]  — nested fills

    Returns None if no fill price can be determined.
    """
    if not isinstance(result, dict):
        return None

    # Direct averagePrice field
    avg = result.get("averagePrice")
    if avg is not None:
        try:
            return float(avg)
        except (TypeError, ValueError):
            pass

    # Walk into nested "order" dict
    order = result.get("order")
    if isinstance(order, dict):
        avg = order.get("averagePrice")
        if avg is not None:
            try:
                return float(avg)
            except (TypeError, ValueError):
                pass

    # Process fills list
    fills = result.get("fills") or []
    if order and isinstance(order, dict) and not fills:
        fills = order.get("fills") or []

    if fills and isinstance(fills, list):
        prices = []
        for f in fills:
            if not isinstance(f, dict):
                continue
            p = f.get("price") or f.get("p")
            if p is not None:
                try:
                    prices.append(float(p))
                except (TypeError, ValueError):
                    continue
        if prices:
            return sum(prices) / len(prices)

    # Fallback: limit price
    price = result.get("price")
    if price is not None:
        try:
            return float(price)
        except (TypeError, ValueError):
            pass

    if order and isinstance(order, dict):
        price = order.get("price")
        if price is not None:
            try:
                return float(price)
            except (TypeError, ValueError):
                pass

    return None


def direct_polymarket_trade(side, amount, price, clob_token_ids, fee_rate_bps=0, condition_id=None, market_id=None,
                            order_type_override=None, post_only=False, mock=False):
    """Place a BUY order directly on Polymarket CLOB V2 using the side token id.

    Follows Polymarket's current py-clob-client-v2 flow:
    - outcome token ids are used to build orders
    - token id is used to resolve tick size / negative risk / parent market
    - condition_id is used only when querying market metadata
    - FAK/FOK orders use MarketOrderArgs where amount is USD for BUY orders
    - GTC PostOnly orders are placed when post_only=True

    Args:
        side: "yes" or "no"
        amount: USD amount to spend
        price: limit price or market-order worst acceptable price (0-1)
        clob_token_ids: [yes_token_id, no_token_id]
        fee_rate_bps: retained for compatibility; V2 SDK resolves fees internally
        condition_id: Polymarket condition_id, if already known
        market_id: display ID (can be slug or UUID, NOT used for CLOB API calls)
        order_type_override: Force GTC/FAK/FOK/GTD (default: from config ORDER_TYPE)
        post_only: If True, set PostOnly on GTC orders (for resting hedge orders)
    """
    if not clob_token_ids or len(clob_token_ids) < 2:
        return {"success": False, "error": "missing_clob_token_ids"}
    token_id = clob_token_ids[0] if side.lower() == "yes" else clob_token_ids[1]
    if not token_id:
        return {"success": False, "error": f"missing_{side.lower()}_token_id"}
    if price <= 0 or price >= 1:
        return {"success": False, "error": f"invalid_direct_price:{price}"}
    if mock:
        return {
            "success": True,
            "mock": True,
            "side": side.lower(),
            "amount": amount,
            "fill_price": price,
            "shares": (amount / price) if price else None,
            "order_id": f"MOCK-{side.lower()}-{str(condition_id or token_id)[:12]}",
            "trade_id": f"MOCK-{side.lower()}-{str(condition_id or token_id)[:12]}",
        }

    try:
        from py_clob_client_v2 import (
            MarketOrderArgs, OrderArgs, OrderType, PartialCreateOrderOptions,
        )
        from py_clob_client_v2.order_builder.constants import BUY
    except ImportError as e:
        return {"success": False, "error": f"py_clob_client_v2_unavailable:{e}"}

    MAX_RETRIES = 2

    def _is_version_mismatch_error(result):
        """Check if the result indicates an order_version_mismatch error."""
        if isinstance(result, dict):
            err = result.get("error_message") or result.get("error") or {}
            if isinstance(err, dict) and "order_version_mismatch" in str(err.get("error", "")):
                return True
            err_str = str(err)
            if "order_version_mismatch" in err_str or "version_mismatch" in err_str:
                return True
        err_str = str(result) if result else ""
        return "order_version_mismatch" in err_str or "version_mismatch" in err_str

    def _is_market_closed_error(result):
        """Check if the error indicates the market is closed/resolved."""
        err_str = str(result) if result else ""
        closed_markers = (
            "market closed", "market resolved", "not accepting orders",
            "accepting_orders", "closed", "resolved", "expired", "cancel-only",
            "market not found", "not found",
        )
        return any(marker in err_str.lower() for marker in closed_markers)

    def _is_market_resolved(result):
        """Check if the error indicates the market is resolved."""
        err_str = str(result) if result else ""
        resolved_markers = ("resolved", "closed", "expired")
        return any(marker in err_str.lower() for marker in resolved_markers)

    def _is_accepting_orders(market_info):
        """Check if market is still accepting orders based on market info dict."""
        if not isinstance(market_info, dict):
            return True  # Assume accepting if we can't determine
        def _truthy(value, default=True):
            if value is None:
                return default
            if isinstance(value, str):
                return value.strip().lower() not in {"false", "0", "no", "closed"}
            return bool(value)

        # Check various indicators that market is closed/resolved
        accepting = (
            market_info.get("accepting_orders")
            if "accepting_orders" in market_info
            else market_info.get("acceptingOrders", market_info.get("acceptingOrdersRaw", True))
        )
        if not _truthy(accepting):
            return False
        if _truthy(market_info.get("closed"), default=False):
            return False
        if _truthy(market_info.get("resolved"), default=False):
            return False
        if _truthy(market_info.get("marketClosed"), default=False):
            return False
        if not _truthy(market_info.get("active"), default=True):
            return False
        return True

    def _extract_condition_id(market_info):
        if not isinstance(market_info, dict):
            return None
        for key in ("condition_id", "conditionId", "conditionID", "market", "market_id"):
            value = market_info.get(key)
            if value:
                return str(value)
        return None

    def _resolve_market_context(client, token_id, condition_id=None):
        """Resolve condition_id, tick size, neg risk, and accepting status from V2 APIs."""
        market_info = None
        resolved_condition_id = condition_id

        if resolved_condition_id:
            try:
                market_info = client.get_market(resolved_condition_id)
            except Exception as e:
                err_str = str(e).lower()
                if "not found" not in err_str and "market not found" not in err_str:
                    return None, f"market_status_check_failed:{e}"

        if market_info is None:
            market_info = fetch_market_by_token(token_id)
            token_condition_id = _extract_condition_id(market_info)
            if token_condition_id:
                resolved_condition_id = token_condition_id

        if not resolved_condition_id:
            return None, f"missing_condition_id_for_token:{token_id}"

        if market_info is None:
            try:
                market_info = client.get_market(resolved_condition_id)
            except Exception as e:
                return None, f"market_status_check_failed:{e}"

        if not _is_accepting_orders(market_info):
            return None, f"market_not_accepting_orders:{resolved_condition_id}"

        try:
            tick_size = str(market_info.get("minimum_tick_size") or market_info.get("minimumTickSize") or client.get_tick_size(token_id))
            neg_risk = market_info.get("neg_risk")
            if neg_risk is None:
                neg_risk = market_info.get("negRisk")
            if neg_risk is None:
                neg_risk = client.get_neg_risk(token_id)
        except Exception as e:
            return None, f"market_order_options_failed:{e}"

        return {
            "condition_id": resolved_condition_id,
            "market_info": market_info,
            "options": PartialCreateOrderOptions(tick_size=tick_size, neg_risk=bool(neg_risk)),
        }, None

    def _clob_result_success(result):
        if not isinstance(result, dict):
            return False, f"unexpected_clob_response:{result}"
        if result.get("success") is False:
            return False, result.get("errorMsg") or result.get("error_msg") or result.get("error") or str(result)
        status = str(result.get("status") or "").lower()
        if status in {"rejected", "failed", "canceled", "cancelled"}:
            return False, result.get("errorMsg") or result.get("error_msg") or f"clob_order_{status}"
        if result.get("error") or result.get("error_message"):
            return False, result.get("error") or result.get("error_message")
        return True, None

    def _order_type_name(order_type):
        return getattr(order_type, "name", str(order_type)).upper()

    def _is_no_liquidity_error(value):
        err_str = str(value or "").lower()
        no_liquidity_markers = (
            "no orders found to match with fak order",
            "ord_reject_reason_no_liquidity",
            "no liquidity",
            "no match",
        )
        return any(marker in err_str for marker in no_liquidity_markers)

    def _is_auth_error(value):
        err_str = str(value or "").lower()
        auth_markers = (
            "could not create api key",
            "auth/api-key",
            "unauthorized",
            "invalid api key",
            "api key",
        )
        return any(marker in err_str for marker in auth_markers)

    def _clean_no_liquidity_response(order_type_name, detail=None, side_book=None):
        error_code = "direct_clob_fak_no_match" if order_type_name == "FAK" else "direct_clob_no_liquidity"
        response = {
            "success": False,
            "error": error_code,
            "direct_clob": True,
            "skip_reason": error_code,
        }
        if detail:
            response["detail"] = str(detail)
        if side_book:
            response["side_book"] = side_book
        return response

    def _preflight_immediate_fill_quote(order_type, current_price):
        """Re-quote before FAK/FOK submission to avoid sending stale no-fill orders."""
        order_type_name = _order_type_name(order_type)
        if order_type_name not in ("FAK", "FOK"):
            return current_price, None

        side_book = fetch_side_orderbook_price(clob_token_ids, side)
        if not side_book:
            return None, {
                "success": False,
                "error": "direct_clob_no_executable_ask",
                "direct_clob": True,
                "skip_reason": "direct_clob_no_executable_ask",
            }

        best_ask = side_book.get("best_ask")
        try:
            best_ask = float(best_ask)
            limit_price = float(current_price)
        except (TypeError, ValueError):
            return None, {
                "success": False,
                "error": "direct_clob_invalid_requote",
                "direct_clob": True,
                "skip_reason": "direct_clob_invalid_requote",
                "side_book": side_book,
            }

        # Keep the strategy's original price as the worst acceptable price. If
        # the top ask has already moved above it, a FAK/FOK would be killed.
        if best_ask > limit_price + 1e-9:
            return None, {
                "success": False,
                "error": "direct_clob_quote_moved",
                "direct_clob": True,
                "skip_reason": "direct_clob_quote_moved",
                "best_ask": best_ask,
                "limit_price": limit_price,
                "side_book": side_book,
            }

        return limit_price, None

    def _create_and_post_order(client, token_id, order_type, options, order_price):
        """Create, sign, and submit the V2 CLOB order."""
        order_type_name = _order_type_name(order_type)

        if order_type_name in ("FAK", "FOK"):
            order_args = MarketOrderArgs(
                token_id=str(token_id),
                amount=round(float(amount), 2),
                side=BUY,
                price=float(order_price),
                order_type=order_type,
            )
            return client.create_and_post_market_order(
                order_args=order_args,
                options=options,
                order_type=order_type,
            )

        shares = float(amount) / float(order_price)
        order_args = OrderArgs(
            token_id=str(token_id),
            price=float(order_price),
            size=shares,
            side=BUY,
        )
        return client.create_and_post_order(
            order_args=order_args,
            options=options,
            order_type=order_type,
            post_only=force_post_only,
        )

    def _do_trade_with_retry(token_id, order_type, options, order_price, retry_count=0):
        """Execute trade with retry logic for version mismatch errors."""
        if retry_count > 0:
            time.sleep(0.5 * retry_count)

        client = get_direct_clob_client()
        try:
            result = _create_and_post_order(client, token_id, order_type, options, order_price)
        except Exception as e:
            if retry_count < MAX_RETRIES and _is_version_mismatch_error(e):
                reset_direct_clob_client()
                return _do_trade_with_retry(token_id, order_type, options, order_price, retry_count=retry_count + 1)
            if _is_no_liquidity_error(e):
                return _clean_no_liquidity_response(_order_type_name(order_type), detail=e)
            raise

        if _is_market_closed_error(result) or _is_market_resolved(result):
            return {"error": "market_closed_or_resolved", "result": result}

        if retry_count < MAX_RETRIES and _is_version_mismatch_error(result):
            reset_direct_clob_client()
            return _do_trade_with_retry(token_id, order_type, options, order_price, retry_count=retry_count + 1)

        return result

    order_type = None
    order_price = price
    force_post_only = bool(post_only)

    try:
        order_type = _get_order_type_enum(order_type_override)
        order_type_name = _order_type_name(order_type)

        client = get_direct_clob_client()
        market_context, status_error = _resolve_market_context(client, token_id, condition_id=condition_id)
        if not market_context:
            return {
                "success": False,
                "error": status_error,
                "direct_clob": True
            }

        order_price, preflight_error = _preflight_immediate_fill_quote(order_type, price)
        if preflight_error:
            return preflight_error

        result = _do_trade_with_retry(token_id, order_type, market_context["options"], order_price)

        # Check for market closed/resolved error
        if isinstance(result, dict) and result.get("error") == "market_closed_or_resolved":
            return {"success": False, "error": "market_closed_or_resolved", "direct_clob": True}
        if isinstance(result, dict) and result.get("success") is False and result.get("error") in {
            "direct_clob_fak_no_match",
            "direct_clob_no_liquidity",
        }:
            return result

        ok, result_error = _clob_result_success(result)
        if not ok:
            if _is_no_liquidity_error(result_error or result):
                return _clean_no_liquidity_response(order_type_name, detail=result_error or result)
            return {"success": False, "error": f"direct_clob_order_rejected: {result_error}", "direct_clob": True, "clob_order": result}

    except Exception as e:
        err_str = str(e)
        if "order_version_mismatch" in err_str or "version_mismatch" in err_str:
            # Force fresh client on retry
            reset_direct_clob_client()
            try:
                client = get_direct_clob_client()
                market_context, status_error = _resolve_market_context(client, token_id, condition_id=condition_id)
                if not market_context:
                    return {"success": False, "error": status_error, "direct_clob": True}
                order_price, preflight_error = _preflight_immediate_fill_quote(order_type, price)
                if preflight_error:
                    return preflight_error
                result = _do_trade_with_retry(token_id, order_type, market_context["options"], order_price, retry_count=1)
                if isinstance(result, dict) and result.get("error") == "market_closed_or_resolved":
                    return {"success": False, "error": "market_closed_or_resolved", "direct_clob": True}
                ok, result_error = _clob_result_success(result)
                if not ok:
                    if _is_no_liquidity_error(result_error or result):
                        return _clean_no_liquidity_response(_order_type_name(order_type), detail=result_error or result)
                    return {"success": False, "error": f"direct_clob_order_rejected: {result_error}", "direct_clob": True, "clob_order": result}
            except Exception as retry_e:
                return {"success": False, "error": f"direct_clob_retry_failed: {retry_e}", "direct_clob": True}
        if _is_auth_error(e):
            reset_direct_clob_client()
            return {"success": False, "error": f"direct_clob_auth_failed: {e}", "direct_clob": True}
        if _is_no_liquidity_error(e):
            return _clean_no_liquidity_response(_order_type_name(order_type) if order_type else "FAK", detail=e)
        return {"success": False, "error": f"direct_clob_failed: {e}", "direct_clob": True}

    # Extract order ID and build response
    order_id = _extract_order_id(result)
    fill_price = _extract_fill_price(result)
    shares = float(amount) / float(order_price)
    return {
        "success": True,
        "trade_id": order_id,
        "fill_price": fill_price,
        "shares_bought": shares,
        "shares": shares,
        "error": None,
        "simulated": False,
        "direct_clob": True,
        "condition_id": market_context.get("condition_id"),
        "clob_order": result,
    }


# =============================================================================
# Wallet Linking
# =============================================================================

def _is_retryable_wallet_link_error(error_text):
    """Return True when wallet-linking failed due to a transient API/network error."""
    text = (error_text or "").lower()
    retryable_markers = (
        "503", "502", "504",
        "service unavailable", "bad gateway", "gateway timeout",
        "timed out", "timeout", "connection aborted", "connection reset",
    )
    return any(marker in text for marker in retryable_markers)


def _wallet_link_log(message):
    print(message, flush=True)


def _redact_wallet_link_error(error_text):
    text = str(error_text or "")
    wallet = getattr(_client, "_wallet_address", None)
    if wallet:
        text = text.replace(wallet, wallet[:10] + "...")
    return text


def ensure_wallet_linked_with_retry():
    """Pre-link the external wallet before trade() so transient 5xx errors get retried."""
    client = get_client()
    if getattr(client, "venue", None) != "polymarket":
        return True, None
    if not getattr(client, "_private_key", None):
        return True, None
    if getattr(client, "_wallet_linked", None) is True:
        return True, None
    if not hasattr(client, "_ensure_wallet_linked"):
        return True, None

    attempts = max(1, WALLET_LINK_RETRIES)
    delay = max(0.0, WALLET_LINK_RETRY_DELAY)
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            if attempt > 1:
                _wallet_link_log(f"  🔁 Retrying wallet link ({attempt}/{attempts})...")
            client._ensure_wallet_linked()
            return True, None
        except Exception as e:
            last_error = str(e)
            if attempt >= attempts or not _is_retryable_wallet_link_error(last_error):
                break
            sleep_for = delay * attempt
            _wallet_link_log(
                f"  ⏳ Wallet link failed transiently: "
                f"{_redact_wallet_link_error(last_error)}; retrying in {sleep_for:.1f}s"
            )
            time.sleep(sleep_for)

    return False, last_error or "unknown wallet link error"


# =============================================================================
# Order Management
# =============================================================================

def cancel_order(order_id: str, mock: bool = False) -> dict:
    """Cancel an existing CLOB order by order_id. Returns success dict."""
    if not order_id:
        return {"success": False, "error": "missing_order_id"}

    if mock:
        return {"success": True, "order_id": order_id, "cancelled": True, "mock": True}

    # Dry-run / paper simulation: just acknowledge
    if order_id.startswith("DRY-"):
        return {"success": True, "order_id": order_id, "cancelled": True, "simulated": True}

    try:
        client = get_direct_clob_client()
        result = client.cancel_order(order_id)
        if result is True or (isinstance(result, dict) and result.get("success") is not False):
            return {"success": True, "order_id": order_id, "cancelled": True}
        return {"success": False, "error": f"cancel_failed: {result}"}
    except Exception as e:
        err_str = str(e)
        if "order not found" in err_str.lower() or "not found" in err_str.lower():
            # Already filled or cancelled — treat as success
            return {"success": True, "order_id": order_id, "cancelled": True, "note": "not_found_treated_as_cancelled"}
        return {"success": False, "error": f"cancel_order_error: {e}"}
