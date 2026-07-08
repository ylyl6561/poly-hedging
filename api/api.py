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
import io
from datetime import datetime, timezone
from contextlib import redirect_stderr
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from urllib.parse import quote

from accounts import AccountContext, get_account_registry
from core.constants import CLOB_API, TRADE_SOURCE, SKILL_SLUG
from core import (
    DIRECT_CLOB_HOST, DIRECT_CLOB_CHAIN_ID, DIRECT_CLOB_SIGNATURE_TYPE,
    DIRECT_CLOB_FUNDER, WALLET_LINK_RETRIES, WALLET_LINK_RETRY_DELAY,
    ORDER_TYPE,
)
from .clob_client_manager import get_clob_client_manager
from .stderr_utils import (
    call_with_optional_stderr_suppression,
    direct_clob_debug,
    is_direct_clob_debug_enabled,
    should_suppress_known_direct_clob_stderr_line,
)


# =============================================================================
# HTTP Helpers
# =============================================================================

_client = None

def _should_suppress_direct_clob_stderr() -> bool:
    flag = os.environ.get("DIRECT_CLOB_SUPPRESS_STDERR", "")
    return flag.strip().lower() in {"1", "true", "yes", "on"}


def _redact_secret(value: str | None, *, left: int = 6, right: int = 4) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) <= left + right:
        return "*" * len(text)
    return f"{text[:left]}...{text[-right:]}"

def _resolve_direct_clob_funder(account: AccountContext) -> str | None:
    return account.funder_address


def reset_direct_clob_client(*, account: AccountContext | None = None):
    """Reset the cached ClobClient for one account or all accounts."""
    manager = get_clob_client_manager()
    if account is None:
        manager.reset_all()
        return
    manager.reset_client(account)


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

    优化策略：优先使用 CLOB API（结算更快），Gamma API 作为兜底。

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

    # ===== 第一优先级：CLOB API（结算最快）=====
    saw_unsettled = False

    # 1. 直接用 condition_id 查询 CLOB /markets/{id}
    if market_id:
        result = api_request(f"{CLOB_API}/markets/{quote(str(market_id))}", timeout=10)
        if result and isinstance(result, dict) and not result.get("error"):
            extracted = _extract_outcome_from_market(result, market_id=market_id)
            if extracted:
                outcome = extracted.get("outcome") or extracted.get("winner")
                if outcome:
                    extracted["source"] = "clob_direct"
                    return extracted
                if not extracted.get("settled"):
                    saw_unsettled = True

        # 2. 用 condition_id 过滤查询 CLOB /markets?condition_id={id}
        result = api_request(f"{CLOB_API}/markets?condition_id={quote(str(market_id))}", timeout=10)
        if result and isinstance(result, dict) and "data" in result:
            for market in result.get("data", []):
                extracted = _extract_outcome_from_market(market, market_id=market_id)
                if extracted:
                    outcome = extracted.get("outcome") or extracted.get("winner")
                    if outcome:
                        extracted["source"] = "clob_filter"
                        return extracted
                    if not extracted.get("settled"):
                        saw_unsettled = True

    # 3. 用 token_id 查询 CLOB /markets-by-token/{token_id}
    for token_id in clob_token_ids or []:
        result = api_request(f"{CLOB_API}/markets-by-token/{quote(str(token_id))}", timeout=10)
        if result and isinstance(result, dict) and not result.get("error"):
            extracted = _extract_outcome_from_market(result, market_id=market_id)
            if extracted:
                outcome = extracted.get("outcome") or extracted.get("winner")
                if outcome:
                    extracted["source"] = "clob_token"
                    return extracted
                if not extracted.get("settled"):
                    saw_unsettled = True

    # ===== 第二优先级：Gamma API（兜底）=====
    # Gamma API 比 CLOB 慢，但提供更完整的市场信息

    # 1. 用 slug 查询（BTC 5m 市场使用 deterministic slug）
    if slug:
        for url in (
            f"{GAMMA_API}/events/slug/{quote(str(slug))}",
            f"{GAMMA_API}/markets/slug/{quote(str(slug))}",
        ):
            payload = api_request(url, timeout=10)
            if not payload or not isinstance(payload, (dict, list)):
                continue
            if isinstance(payload, dict) and payload.get("error"):
                continue
            for market in _iter_gamma_markets(payload):
                extracted = _extract_outcome_from_market(market, market_id=slug)
                if extracted:
                    outcome = extracted.get("outcome") or extracted.get("winner")
                    if outcome:
                        extracted["source"] = "gamma_slug"
                        return extracted
                    if not extracted.get("settled"):
                        saw_unsettled = True

    # 2. 用 condition_id 查询 Gamma（最后的兜底）
    if market_id:
        for url in (
            f"{GAMMA_API}/markets?conditionId={quote(str(market_id))}",
            f"{GAMMA_API}/markets?condition_ids={quote(str(market_id))}",
            f"{GAMMA_API}/markets?market={quote(str(market_id))}",
        ):
            payload = api_request(url, timeout=10)
            if not payload or not isinstance(payload, (dict, list)):
                continue
            if isinstance(payload, dict) and payload.get("error"):
                continue
            for market in _iter_gamma_markets(payload):
                extracted = _extract_outcome_from_market(market, market_id=market_id)
                if extracted:
                    outcome = extracted.get("outcome") or extracted.get("winner")
                    if outcome:
                        extracted["source"] = "gamma_condition"
                        return extracted
                    if not extracted.get("settled"):
                        saw_unsettled = True

    # ===== 返回结果 =====
    if saw_unsettled:
        return {
            "outcome": None,
            "winner": None,
            "settled": False,
            "source": "pending",
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
    token_idx = 0 if side in ("yes", "up") else 1 if side in ("no", "down") else None
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
    registry = get_account_registry()
    return (
        not dry_run
        and EXECUTION_ROUTE == "direct_clob"
        and os.environ.get("TRADING_VENUE", "polymarket") == "polymarket"
        and bool(registry.list_accounts())
    )


def get_execution_route():
    from core.config import EXECUTION_ROUTE
    return EXECUTION_ROUTE


def _extract_usdc_balance(payload):
    if payload is None:
        return None

    def _normalize_balance_value(value, *, key: str | None = None, asset_hint: str | None = None):
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        normalized_key = (key or "").lower()
        normalized_asset = str(asset_hint or "").lower()
        if normalized_key in {"balance", "balance_usdc", "available", "available_usdc", "usdc", "usdc_balance", "amount", "free", "freecollateral", "free_collateral", "buyingpower", "buying_power"}:
            if normalized_asset in {"pusd", "usdc", "usdc.e", "collateral"} or numeric >= 1_000_000:
                return numeric / 1_000_000.0
        return numeric

    if isinstance(payload, (int, float)):
        return _normalize_balance_value(payload)
    if isinstance(payload, str):
        return _normalize_balance_value(payload)
    if isinstance(payload, list):
        for item in payload:
            parsed = _extract_usdc_balance(item)
            if parsed is not None:
                return parsed
        return None
    if not isinstance(payload, dict):
        return None

    asset_hint = payload.get("asset") or payload.get("currency") or payload.get("symbol")
    candidate_keys = (
        "balance_usdc", "balance", "available", "available_usdc", "usdc", "usdc_balance",
        "amount", "free", "freeCollateral", "free_collateral", "buyingPower", "buying_power",
        "total", "value",
    )
    for key in candidate_keys:
        value = payload.get(key)
        parsed = _normalize_balance_value(value, key=key, asset_hint=asset_hint)
        if parsed is not None:
            return parsed

    for nested_key in ("balance", "allowance", "data"):
        nested = payload.get(nested_key)
        parsed = _extract_usdc_balance(nested)
        if parsed is not None:
            return parsed
    return None


def _build_collateral_balance_params(signature_type: int):
    try:
        from py_clob_client_v2 import BalanceAllowanceParams, AssetType
    except ImportError:
        try:
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
        except ImportError:
            return None

    try:
        return BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=signature_type,
        )
    except TypeError:
        try:
            return BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        except TypeError:
            try:
                return {"asset_type": getattr(AssetType.COLLATERAL, "value", AssetType.COLLATERAL), "signature_type": signature_type}
            except Exception:
                return {"asset_type": "COLLATERAL", "signature_type": signature_type}

def _call_client_method_variants(client, method_name: str, attempts: list[tuple[tuple, dict]]) -> tuple[object | None, str | None]:
    method = getattr(client, method_name, None)
    if method is None:
        return None, f"method_missing:{method_name}"

    last_error = None
    for args, kwargs in attempts:
        try:
            return call_with_optional_stderr_suppression(method, *args, **kwargs), None
        except TypeError as exc:
            last_error = exc
            continue
        except Exception as exc:
            last_error = exc
            continue
    return None, str(last_error) if last_error else f"{method_name}_unavailable"


def _fetch_balance_allowance_payload(client, *, signature_type: int):
    params = _build_collateral_balance_params(signature_type)
    attempts = []
    if params is not None:
        attempts.append(((), {"params": params}))
        attempts.append(((params,), {}))
        if isinstance(params, dict):
            attempts.append(((), params))
    attempts.extend([
        ((), {"params": {"asset_type": "COLLATERAL", "signature_type": signature_type}}),
        (({"asset_type": "COLLATERAL", "signature_type": signature_type},), {}),
        ((), {"params": {"asset_type": "COLLATERAL"}}),
        (({"asset_type": "COLLATERAL"},), {}),
        ((), {}),
        ((), {"params": {}}),
    ])

    payload, error = _call_client_method_variants(client, "get_balance_allowance", attempts)
    if error is not None:
        return {"error": error or "balance_allowance_unavailable"}
    return payload


def _fetch_balance_probe_payloads(client, *, signature_type: int) -> dict[str, dict[str, object]]:
    params = _build_collateral_balance_params(signature_type)
    probe_specs: list[tuple[str, str, list[tuple[tuple, dict] | None]]] = [
        (
            "get_balance_allowance",
            "sdk_get_balance_allowance",
            [
                ((), {"params": params}) if params is not None else None,
                ((params,), {}) if params is not None else None,
                ((), params) if isinstance(params, dict) else None,
                ((), {"params": {"asset_type": "COLLATERAL", "signature_type": signature_type}}),
                (({"asset_type": "COLLATERAL", "signature_type": signature_type},), {}),
                ((), {"params": {"asset_type": "COLLATERAL"}}),
                (({"asset_type": "COLLATERAL"},), {}),
                ((), {}),
            ],
        ),
        (
            "get_balance",
            "sdk_get_balance",
            [
                ((), {"params": params}) if params is not None else None,
                ((params,), {}) if params is not None else None,
                ((), params) if isinstance(params, dict) else None,
                ((), {"params": {"asset_type": "COLLATERAL", "signature_type": signature_type}}),
                (({"asset_type": "COLLATERAL", "signature_type": signature_type},), {}),
                ((), {"params": {"asset_type": "COLLATERAL"}}),
                (({"asset_type": "COLLATERAL"},), {}),
                ((), {}),
            ],
        ),
        (
            "get_collateral",
            "sdk_get_collateral",
            [
                ((), {}),
                ((), {"params": params}) if params is not None else None,
                ((params,), {}) if params is not None else None,
            ],
        ),
        (
            "get_usdc_balance",
            "sdk_get_usdc_balance",
            [
                ((), {}),
                ((), {"params": params}) if params is not None else None,
                ((params,), {}) if params is not None else None,
            ],
        ),
    ]

    payloads: dict[str, dict[str, object]] = {}
    for method_name, label, raw_attempts in probe_specs:
        attempts = [attempt for attempt in raw_attempts if attempt is not None]
        payload, error = _call_client_method_variants(client, method_name, attempts)
        payloads[label] = {
            "payload": payload,
            "error": error,
            "parsed_balance_usdc": _extract_usdc_balance(payload) if error is None else None,
            "payload_type": type(payload).__name__ if error is None and payload is not None else None,
        }
    return payloads


def get_wallet_usdc_balance(*, account: AccountContext) -> dict:
    wallet_address = account.wallet_address

    def _emit_balance_debug(event: str, **fields) -> None:
        preview = fields.pop("raw_preview", None)
        if preview is None and "raw" in fields:
            try:
                raw_text = json.dumps(fields["raw"], ensure_ascii=False, default=str)
            except Exception:
                raw_text = repr(fields["raw"])
            preview = raw_text[:1200]
            fields.pop("raw", None)
        direct_clob_debug(
            event,
            account_id=account.account_id,
            label=account.label,
            wallet_address=wallet_address,
            **fields,
            raw_preview=preview,
        )

    try:
        client = get_direct_clob_client(account=account)
    except Exception as exc:
        _emit_balance_debug("wallet_balance_client_init_failed", error=repr(exc))
        return {
            "success": False,
            "error": f"clob_client_init_failed:{exc}",
            "wallet_address": wallet_address,
            "account_id": account.account_id,
        }

    payload = _fetch_balance_allowance_payload(client, signature_type=account.signature_type)
    parsed = _extract_usdc_balance(payload)
    _emit_balance_debug(
        "wallet_balance_balance_allowance_result",
        parsed_balance_usdc=parsed,
        payload_type=type(payload).__name__,
        raw=payload,
    )
    if parsed is not None and parsed > 0:
        return {
            "success": True,
            "wallet_address": wallet_address,
            "balance_usdc": parsed,
            "raw": payload,
            "account_id": account.account_id,
            "label": account.label,
        }

    probe_payloads = _fetch_balance_probe_payloads(client, signature_type=account.signature_type)
    for probe_label, probe_result in probe_payloads.items():
        _emit_balance_debug(
            "wallet_balance_sdk_probe_result",
            probe=probe_label,
            error=probe_result.get("error"),
            parsed_balance_usdc=probe_result.get("parsed_balance_usdc"),
            payload_type=probe_result.get("payload_type"),
            raw=probe_result.get("payload"),
        )

    positive_probe = next(
        (
            probe_result
            for probe_result in probe_payloads.values()
            if probe_result.get("error") is None
            and probe_result.get("parsed_balance_usdc") is not None
            and float(probe_result.get("parsed_balance_usdc") or 0) > 0
        ),
        None,
    )
    if positive_probe is not None:
        return {
            "success": True,
            "wallet_address": wallet_address,
            "balance_usdc": float(positive_probe["parsed_balance_usdc"]),
            "raw": positive_probe.get("payload"),
            "account_id": account.account_id,
            "label": account.label,
        }

    if parsed is not None:
        return {
            "success": True,
            "wallet_address": wallet_address,
            "balance_usdc": parsed,
            "raw": payload,
            "account_id": account.account_id,
            "label": account.label,
        }

    value_payload = api_request(f"https://data-api.polymarket.com/value?user={quote(str(wallet_address))}", timeout=10)
    parsed = _extract_usdc_balance(value_payload)
    _emit_balance_debug(
        "wallet_balance_value_api_result",
        parsed_balance_usdc=parsed,
        payload_type=type(value_payload).__name__,
        raw=value_payload,
    )
    if parsed is not None:
        return {
            "success": True,
            "wallet_address": wallet_address,
            "balance_usdc": parsed,
            "raw": value_payload,
            "account_id": account.account_id,
            "label": account.label,
        }

    _emit_balance_debug("wallet_balance_unavailable")
    return {
        "success": False,
        "wallet_address": wallet_address,
        "account_id": account.account_id,
        "label": account.label,
        "error": "balance_unavailable",
    }


# =============================================================================
# Token Balance (CONDITIONAL asset) — for sell-side on-chain reconciliation
# =============================================================================
#
# 背景：
#   Polymarket V2 CLOB 的 ``status="matched"`` 仅代表 off-chain 撮合成功，
#   token 异步通过 CTF Exchange 转账到 proxy wallet。在 5m BTC 等快市场上，
#   GTC BUY 经常报告 matched 但 ``CTF.balanceOf`` 仍为 0（issue #54 / #328）。
#   因此在抛售单挂出之前必须直接读 on-chain token balance 做对账，避免
#   CLOB 打回 ``not enough balance / allowance``。
#
# 单位：Polymarket CLOB 的 CONDITIONAL token 与 orderbook 用同样的 6 位精度，
# 因此 ``raw_balance / 1_000_000.0`` = shares（人类可读）。

_CONDITIONAL_BALANCE_PARAMS_BUILDERS = (
    # (asset_id, signature_type, funder_address) — V2 SDK 主路径
    ("balance_allowance_v2_signature", lambda asset_id, sig, funder: _try_build_conditional_params_v2(asset_id, sig, funder)),
    # 仅传 asset_id + funder
    ("balance_allowance_asset_only", lambda asset_id, sig, funder: _try_build_conditional_params(asset_id, sig, with_signature=False, funder_address=funder)),
    # 仅传 signature_type + funder（兜底）
    ("balance_allowance_signature_only", lambda asset_id, sig, funder: _try_build_conditional_params(asset_id, sig, with_asset=False, funder_address=funder)),
    # 仅传 signature_type（无 asset_id 的兜底）
    ("balance_allowance_signature_only_fallback", lambda asset_id, sig, funder: _try_build_conditional_params(asset_id, sig, with_asset=False, with_signature=True, funder_address=funder)),
)


def _try_build_conditional_params_v2(asset_id: str, signature_type: int, funder_address: str | None = None):
    """尝试用 V2 SDK 的 BalanceAllowanceParams + AssetType.CONDITIONAL 构造参数。"""
    try:
        from py_clob_client_v2 import BalanceAllowanceParams, AssetType
    except ImportError:
        try:
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
        except ImportError:
            return None
    try:
        return ((), {"params": BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL,
            token_id=str(asset_id),
            signature_type=signature_type,
        )})
    except TypeError:
        return None


def _try_build_conditional_params(asset_id: str, signature_type: int, *, with_asset: bool = True, with_signature: bool = True, funder_address: str | None = None):
    """降级路径：用 BalanceAllowanceParams 构造 asset_type=CONDITIONAL + token_id / signature_type。"""
    try:
        from py_clob_client_v2 import BalanceAllowanceParams, AssetType
    except ImportError:
        try:
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
        except ImportError:
            return None
    try:
        kwargs: dict = {
            "asset_type": AssetType.CONDITIONAL,
            "signature_type": signature_type,
        }
        if with_asset:
            kwargs["token_id"] = str(asset_id)
        return ((), {"params": BalanceAllowanceParams(**kwargs)})
    except TypeError:
        return None


def _extract_token_balance(payload) -> float | None:
    """从 get_balance_allowance 响应中解析 CONDITIONAL token 余额（shares）。"""
    if payload is None:
        return None
    if isinstance(payload, (int, float)):
        try:
            return float(payload) / 1_000_000.0
        except (TypeError, ValueError):
            return None
    if isinstance(payload, str):
        try:
            return float(payload) / 1_000_000.0
        except ValueError:
            return None
    if isinstance(payload, list):
        for item in payload:
            parsed = _extract_token_balance(item)
            if parsed is not None:
                return parsed
        return None
    if not isinstance(payload, dict):
        return None

    candidate_keys = (
        "balance", "available", "amount", "free", "value",
        "token_balance", "asset_balance", "size",
        # 嵌套结构兜底
    )
    for key in candidate_keys:
        value = payload.get(key)
        if value is None:
            continue
        try:
            return float(value) / 1_000_000.0
        except (TypeError, ValueError):
            continue

    for nested_key in ("balance", "data", "allowance"):
        nested = payload.get(nested_key)
        parsed = _extract_token_balance(nested)
        if parsed is not None:
            return parsed
    return None


def fetch_token_balance(*, asset_id: str, account: AccountContext, mock: bool = False) -> dict:
    """读取 wallet 在某 CONDITIONAL token 上的 CLOB 余额（shares）。

    Returns:
        dict 含 success / balance_shares（浮点 shares） / raw / error 等字段。
        失败时 ``success=False`` 且 ``balance_shares=None``。
    """
    if not asset_id:
        return {
            "success": False,
            "asset_id": asset_id,
            "error": "missing_asset_id",
            "balance_shares": None,
            "account_id": account.account_id,
        }
    if mock:
        return {
            "success": True,
            "asset_id": asset_id,
            "balance_shares": 0.0,
            "raw": {"mock": True},
            "account_id": account.account_id,
            "label": account.label,
        }

    try:
        client = get_direct_clob_client(account=account)
    except Exception as exc:
        return {
            "success": False,
            "asset_id": asset_id,
            "error": f"clob_client_init_failed:{exc}",
            "balance_shares": None,
            "account_id": account.account_id,
        }

    # 多种调用形态依次尝试
    method = getattr(client, "get_balance_allowance", None)
    if method is None:
        return {
            "success": False,
            "asset_id": asset_id,
            "error": "method_missing:get_balance_allowance",
            "balance_shares": None,
            "account_id": account.account_id,
        }

    last_error: str | None = None
    for label, builder in _CONDITIONAL_BALANCE_PARAMS_BUILDERS:
        built = builder(asset_id, account.signature_type, account.funder_address)
        if built is None:
            continue
        args, kwargs = built
        try:
            payload = method(*args, **kwargs)
        except TypeError as exc:
            last_error = f"{label}:{exc}"
            continue
        except Exception as exc:
            last_error = f"{label}:{exc}"
            continue
        parsed = _extract_token_balance(payload)
        if parsed is not None:
            return {
                "success": True,
                "asset_id": asset_id,
                "balance_shares": parsed,
                "raw": payload,
                "probe": label,
                "account_id": account.account_id,
                "label": account.label,
            }
        # 解析不出但没抛错 → 记录后继续尝试下一种形态
        last_error = f"{label}:parse_failed"

    return {
        "success": False,
        "asset_id": asset_id,
        "error": last_error or "balance_unavailable",
        "balance_shares": None,
        "account_id": account.account_id,
        "label": account.label,
    }


# =============================================================================
# Direct Polymarket CLOB Client
# =============================================================================

def get_direct_clob_client(*, account: AccountContext):
    """Create or reuse an authenticated Polymarket CLOB V2 client for one account."""
    direct_clob_debug(
        "create_client_attempt",
        account_id=account.account_id,
        wallet_address=account.wallet_address,
        funder=account.funder_address,
        proxy_address=account.proxy_address,
        funder_env=account.funder_env,
        host=account.host,
        chain_id=account.chain_id,
        signature_type=account.signature_type,
    )
    return get_clob_client_manager().get_client(account)


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
                            order_type_override=None, post_only=False, mock=False, account: AccountContext | None = None,
                            trade_side: str = "buy"):
    """Place a BUY or SELL order directly on Polymarket CLOB V2 using the side token id.

    `side` (legacy positional) selects the outcome token ("yes"/"up" -> clob_token_ids[0],
    "no"/"down" -> clob_token_ids[1]). `trade_side` selects the order direction
    ("buy" = open/increase position, "sell" = close/decrease position).

    For BUY: limit `OrderArgs.size=shares`, market `MarketOrderArgs.amount=USD`.
    For SELL: limit `OrderArgs.size=shares` (tokens sold), market `MarketOrderArgs.amount=tokens_sold`
    (Polymarket V2 MarketOrderArgs.amount is the token count when side=SELL).

    Follows Polymarket's current py-clob-client-v2 flow:
    - outcome token ids are used to build orders
    - token id is used to resolve tick size / negative risk / parent market
    - condition_id is used only when querying market metadata
    - FAK/FOK orders use MarketOrderArgs where amount units depend on trade_side
    - GTC PostOnly orders are placed when post_only=True

    Args:
        side: "yes"/"up" or "no"/"down" (selects which outcome token)
        amount: USD amount to spend (BUY) OR token count to sell (SELL) — but for SELL limit
            orders the caller should pass token count, and the function will compute shares
            accordingly. For SELL market orders, amount is the number of tokens to sell.
        price: limit price or market-order worst acceptable price (0-1)
        clob_token_ids: [yes_token_id, no_token_id]
        fee_rate_bps: retained for compatibility; V2 SDK resolves fees internally
        condition_id: Polymarket condition_id, if already known
        market_id: display ID (can be slug or UUID, NOT used for CLOB API calls)
        order_type_override: Force GTC/FAK/FOK/GTD (default: from config ORDER_TYPE)
        post_only: If True, set PostOnly on GTC orders (for resting hedge orders)
        trade_side: "buy" or "sell" (selects the CLOB order direction)
    """
    trade_side_norm = str(trade_side or "buy").lower()
    if trade_side_norm not in {"buy", "sell"}:
        return {"success": False, "error": f"invalid_trade_side:{trade_side_norm}"}
    is_sell = trade_side_norm == "sell"
    if not clob_token_ids or len(clob_token_ids) < 2:
        return {"success": False, "error": "missing_clob_token_ids"}
    token_id = clob_token_ids[0] if side.lower() in ("yes", "up") else clob_token_ids[1]
    if account is None:
        raise ValueError("direct_polymarket_trade requires account")
    if not token_id:
        return {"success": False, "error": f"missing_{side.lower()}_token_id"}
    if price is None:
        if order_type_override is not None:
            return {"success": False, "error": f"invalid_direct_price:{price}"}
        # 纯市价单（order_type_override=None）：price 无意义，跳过校验
    elif price <= 0 or price >= 1:
        return {"success": False, "error": f"invalid_direct_price:{price}"}
    if mock:
        # Mock: BUY pays USD `amount` to buy (amount/price) tokens; SELL sells `amount` tokens at price.
        if is_sell:
            mock_shares = float(amount or 0.0)
            mock_amount = mock_shares
        else:
            mock_shares = (amount / price) if (price and amount) else None
            mock_amount = amount
        return {
            "success": True,
            "mock": True,
            "side": side.lower(),
            "trade_side": trade_side_norm,
            "amount": mock_amount,
            "fill_price": price,
            "shares": mock_shares,
            "shares_bought": mock_shares if not is_sell else None,
            "shares_sold": mock_shares if is_sell else None,
            "order_id": f"MOCK-{trade_side_norm}-{side.lower()}-{str(condition_id or token_id)[:12]}",
            "trade_id": f"MOCK-{trade_side_norm}-{side.lower()}-{str(condition_id or token_id)[:12]}",
        }

    try:
        from py_clob_client_v2 import (
            MarketOrderArgs, OrderArgs, OrderType, PartialCreateOrderOptions,
        )
        from py_clob_client_v2.order_builder.constants import BUY, SELL
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
        """Re-quote before FAK/FOK submission to avoid sending stale no-fill orders.

        For BUY (trade_side=buy): requires best_ask <= limit_price (we can buy at ask).
        For SELL (trade_side=sell): requires best_bid >= limit_price (we can sell at bid).
        """
        order_type_name = _order_type_name(order_type)
        if order_type_name not in ("FAK", "FOK"):
            return current_price, None

        side_book = fetch_side_orderbook_price(clob_token_ids, side)
        if not side_book:
            error_code = "direct_clob_no_executable_ask" if not is_sell else "direct_clob_no_executable_bid"
            return None, {
                "success": False,
                "error": error_code,
                "direct_clob": True,
                "skip_reason": error_code,
            }

        # For BUY we need best_ask (price sellers ask); for SELL we need best_bid (price buyers bid)
        quote_key = "best_ask" if not is_sell else "best_bid"
        quote_price = side_book.get(quote_key)
        try:
            quote_price = float(quote_price)
            limit_price = float(current_price)
        except (TypeError, ValueError):
            return None, {
                "success": False,
                "error": "direct_clob_invalid_requote",
                "direct_clob": True,
                "skip_reason": "direct_clob_invalid_requote",
                "side_book": side_book,
            }

        # BUY: best_ask must be <= limit_price (we can buy at-or-below our worst acceptable).
        # SELL: best_bid must be >= limit_price (we can sell at-or-above our worst acceptable).
        # If quote moved unfavorably beyond our limit, the FAK/FOK would be killed.
        if not is_sell:
            if quote_price > limit_price + 1e-9:
                return None, {
                    "success": False,
                    "error": "direct_clob_quote_moved",
                    "direct_clob": True,
                    "skip_reason": "direct_clob_quote_moved",
                    "best_ask": quote_price,
                    "limit_price": limit_price,
                    "side_book": side_book,
                }
        else:
            if quote_price + 1e-9 < limit_price:
                return None, {
                    "success": False,
                    "error": "direct_clob_quote_moved",
                    "direct_clob": True,
                    "skip_reason": "direct_clob_quote_moved",
                    "best_bid": quote_price,
                    "limit_price": limit_price,
                    "side_book": side_book,
                }

        return limit_price, None

    def _create_and_post_order(client, token_id, order_type, options, order_price):
        """Create, sign, and submit the V2 CLOB order.

        For BUY (trade_side=buy): MarketOrderArgs.amount is USD; limit OrderArgs.size is shares.
        For SELL (trade_side=sell): MarketOrderArgs.amount is token count to sell;
        limit OrderArgs.size is shares (tokens sold).
        """
        order_type_name = _order_type_name(order_type)
        order_side_const = SELL if is_sell else BUY

        # Pure market order: use create_and_post_market_order without FAK/FOK constraint
        # This allows partial fills instead of all-or-nothing
        if is_pure_market:
            # Polymarket CLOB 的市价单实际是"最差成交价"参数：
            # BUY: price=0.99 表示愿意以 <=0.99 的价格买入（0.99 以下全吃）
            # SELL: price=0.01 表示愿意以 >=0.01 的价格卖出（0.01 以上全卖）
            # 撮合引擎会按最优盘口价依次成交
            if order_price is None:
                order_price = 0.01 if is_sell else 0.99
            order_args = MarketOrderArgs(
                token_id=str(token_id),
                amount=round(float(amount), 2) if not is_sell else round(float(amount), 4),
                side=order_side_const,
                price=float(order_price),
            )
            return client.create_and_post_market_order(
                order_args=order_args,
                options=options,
            )

        if order_type_name in ("FAK", "FOK"):
            # SELL market order: amount must be the token count to sell (not USD).
            # We trust the caller to pass the right units; for SELL callers pass token count
            # (already converted in place_sell). For BUY callers pass USD.
            order_args = MarketOrderArgs(
                token_id=str(token_id),
                amount=round(float(amount), 2) if not is_sell else round(float(amount), 4),
                side=order_side_const,
                price=float(order_price),
                order_type=order_type,
            )
            return client.create_and_post_market_order(
                order_args=order_args,
                options=options,
                order_type=order_type,
            )

        # Limit order: size is always shares (tokens). For BUY, shares = USD/price.
        # For SELL, amount is already the share count to sell.
        if is_sell:
            shares = float(amount)
        else:
            shares = float(amount) / float(order_price)
        order_args = OrderArgs(
            token_id=str(token_id),
            price=float(order_price),
            size=shares,
            side=order_side_const,
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

        client = get_direct_clob_client(account=account)
        try:
            result = _create_and_post_order(client, token_id, order_type, options, order_price)
        except Exception as e:
            if retry_count < MAX_RETRIES and _is_version_mismatch_error(e):
                reset_direct_clob_client(account=account)
                return _do_trade_with_retry(token_id, order_type, options, order_price, retry_count=retry_count + 1)
            if _is_no_liquidity_error(e):
                return _clean_no_liquidity_response(_order_type_name(order_type), detail=e)
            raise

        if _is_market_closed_error(result) or _is_market_resolved(result):
            return {"error": "market_closed_or_resolved", "result": result}

        if retry_count < MAX_RETRIES and _is_version_mismatch_error(result):
            reset_direct_clob_client(account=account)
            return _do_trade_with_retry(token_id, order_type, options, order_price, retry_count=retry_count + 1)

        return result

    order_type = None
    order_price = price
    force_post_only = bool(post_only)
    is_pure_market = order_type_override is None  # True = 纯市价单，无 FAK/FOK 限制

    try:
        order_type = _get_order_type_enum(order_type_override)
        order_type_name = _order_type_name(order_type)

        client = get_direct_clob_client(account=account)
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
            reset_direct_clob_client(account=account)
            try:
                client = get_direct_clob_client(account=account)
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
            reset_direct_clob_client(account=account)
            return {"success": False, "error": f"direct_clob_auth_failed: {e}", "direct_clob": True}
        if _is_no_liquidity_error(e):
            return _clean_no_liquidity_response(_order_type_name(order_type) if order_type else "FAK", detail=e)
        return {"success": False, "error": f"direct_clob_failed: {e}", "direct_clob": True}

    # Extract order ID and build response
    order_id = _extract_order_id(result)
    fill_price = _extract_fill_price(result)
    if is_sell:
        # SELL: amount is the token count we tried to sell; shares_sold = token count.
        shares = float(amount)
    else:
        # BUY: amount is USD, shares_bought = amount / price.
        shares = float(amount) / float(order_price)
    response = {
        "success": True,
        "trade_id": order_id,
        "fill_price": fill_price,
        "trade_side": trade_side_norm,
        "error": None,
        "simulated": False,
        "direct_clob": True,
        "condition_id": market_context.get("condition_id"),
        "token_id": token_id,  # Include token_id for logging/debugging
        "clob_order": result,
    }
    if is_sell:
        response["shares_sold"] = shares
    else:
        response["shares_bought"] = shares
    response["shares"] = shares
    return response


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

def cancel_order(order_id: str, *, account: AccountContext, mock: bool = False) -> dict:
    """Cancel an existing CLOB order by order_id. Returns success dict."""
    if not order_id:
        return {"success": False, "error": "missing_order_id"}

    if mock:
        return {"success": True, "order_id": order_id, "cancelled": True, "mock": True}

    # Dry-run / paper simulation: just acknowledge
    if order_id.startswith("DRY-"):
        return {"success": True, "order_id": order_id, "cancelled": True, "simulated": True}

    try:
        client = get_direct_clob_client(account=account)
        from py_clob_client_v2.clob_types import OrderPayload
        result = client.cancel_order(OrderPayload(orderID=order_id))
        if result is True or (isinstance(result, dict) and result.get("success") is not False):
            return {"success": True, "order_id": order_id, "cancelled": True}
        return {"success": False, "error": f"cancel_failed: {result}"}
    except Exception as e:
        err_str = str(e)
        err_lower = err_str.lower()
        # 已成交/已撤/订单非活跃/市场关闭/无匹配/过期的撤单请求，本质上已经达到"挂单不在簿"的效果，
        # 都应按成功处理（不会再次发起撤单），并把 note 写明便于日志追踪。
        benign_markers = (
            "order not found", "not found",
            "already matched", "already matched or canceled",
            "already canceled", "already cancelled", "already cancelled or matched",
            "order is not active", "not active", "no open order", "no matching order",
            "invalid order", "market closed", "market resolved", "expired",
            "cancel-only", "not accepting orders",
        )
        if any(marker in err_lower for marker in benign_markers):
            return {"success": True, "order_id": order_id, "cancelled": True, "note": f"benign_cancel:{err_str[:200]}"}
        return {"success": False, "error": f"cancel_order_error: {e}"}


def fetch_order_trades(order_id: str, *, account: AccountContext, mock: bool = False) -> dict:
    """批量查询订单的所有成交明细（使用线程池并发获取）。

    1. 获取订单详情
    2. 提取 associate_trades 列表
    3. 使用线程池并发查询每一笔成交的详细时间

    Args:
        order_id: 订单ID
        account: 账户上下文
        mock: 是否为模拟模式

    Returns:
        {
            "success": bool,
            "order_id": str,
            "trades": [  # 按时间排序的成交列表
                {
                    "trade_id": str,
                    "timestamp": str,      # ISO格式
                    "size": float,
                    "price": float,
                    "side": str,
                    "fee": float | None,
                    "raw": dict,
                },
                ...
            ],
            "first_trade_at": str | None,  # ISO格式
            "last_trade_at": str | None,   # ISO格式
            "total_filled_size": float,
            "error": str | None,
        }
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not order_id:
        return {"success": False, "order_id": order_id, "error": "missing_order_id"}

    if mock or order_id.startswith("DRY-"):
        return {
            "success": True,
            "order_id": order_id,
            "trades": [],
            "first_trade_at": None,
            "last_trade_at": None,
            "total_filled_size": 0.0,
        }

    try:
        client = get_direct_clob_client(account=account)

        # 1. 获取订单详情
        order_info = client.get_order(order_id)
        if not isinstance(order_info, dict):
            return {"success": False, "order_id": order_id, "error": f"unexpected_response:{order_info}"}

        # 2. 提取成交 ID 列表
        trade_ids = order_info.get("associate_trades") or []
        if not trade_ids:
            return {
                "success": True,
                "order_id": order_id,
                "trades": [],
                "first_trade_at": None,
                "last_trade_at": None,
                "total_filled_size": 0.0,
            }

        # 3. 使用线程池并发查询每一笔成交
        def _fetch_single_trade(trade_id: str) -> dict | None:
            try:
                from py_clob_client.v2.clob import TradeParams
                trade_detail = client.get_trades(TradeParams(id=trade_id))
                if isinstance(trade_detail, dict):
                    return trade_detail
                elif isinstance(trade_detail, list) and trade_detail:
                    return trade_detail[0]
                return None
            except Exception:
                return None

        trades_with_details: list[dict] = []
        max_workers = min(len(trade_ids), 8)

        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="trade-fetch") as pool:
            futures = {pool.submit(_fetch_single_trade, tid): tid for tid in trade_ids}
            for fut in as_completed(futures):
                result = fut.result()
                if result:
                    trades_with_details.append(result)

        # 4. 解析并排序
        parsed_trades = []
        for trade in trades_with_details:
            ts = trade.get("timestamp") or trade.get("time") or trade.get("created_at")
            size = trade.get("size") or trade.get("filled_size") or trade.get("amount")
            price = trade.get("price") or trade.get("fill_price")
            side = trade.get("side")
            fee = trade.get("fee") or trade.get("fee_amount")

            ts_float = None
            if ts is not None:
                try:
                    ts_float = float(ts)
                    if ts_float > 1e12:
                        ts_float = ts_float / 1000.0
                except (TypeError, ValueError):
                    pass

            parsed_trades.append({
                "trade_id": trade.get("trade_id") or trade.get("id"),
                "timestamp": ts,
                "timestamp_float": ts_float,
                "size": float(size) if size is not None else None,
                "price": float(price) if price is not None else None,
                "side": side,
                "fee": float(fee) if fee is not None else None,
                "raw": trade,
            })

        # 按时间排序
        parsed_trades.sort(key=lambda x: x.get("timestamp_float") or 0)

        # 5. 计算汇总
        total_filled = sum(t.get("size") or 0 for t in parsed_trades if t.get("size"))

        # 提取时间（转换为 ISO 格式）
        from datetime import datetime, timezone
        first_ts = None
        last_ts = None

        for t in parsed_trades:
            tf = t.get("timestamp_float")
            if tf is not None:
                if first_ts is None or tf < first_ts:
                    first_ts = tf
                if last_ts is None or tf > last_ts:
                    last_ts = tf

        first_trade_at = datetime.fromtimestamp(first_ts, tz=timezone.utc).isoformat() if first_ts else None
        last_trade_at = datetime.fromtimestamp(last_ts, tz=timezone.utc).isoformat() if last_ts else None

        # 清理输出（移除内部字段）
        for t in parsed_trades:
            t.pop("timestamp_float", None)

        return {
            "success": True,
            "order_id": order_id,
            "trades": parsed_trades,
            "first_trade_at": first_trade_at,
            "last_trade_at": last_trade_at,
            "total_filled_size": total_filled,
        }

    except Exception as e:
        err_str = str(e)
        err_lower = err_str.lower()

        transient_markers = (
            "request exception", "connection", "timeout", "timed out",
            "network", "read timed out", "connect timed out",
            "503", "502", "504",
        )
        if any(marker in err_lower for marker in transient_markers):
            return {"success": False, "order_id": order_id, "error": f"transient_error:{err_str[:200]}", "retryable": True}

        return {"success": False, "order_id": order_id, "error": f"fetch_order_trades_error:{err_str[:200]}"}


def _is_transient_exception(e: Exception) -> bool:
    """判断异常是否为临时性网络错误。"""
    err_type = type(e).__name__.lower()
    err_msg = str(e).lower()

    # Polymarket SDK 的网络异常
    if "polyapiexception" in err_type or "polyapiexception" in err_msg:
        return True

    # urllib 网络错误
    transient_types = ("httperror", "urlerror", "timeouterror", "connectionerror")
    if any(t in err_type for t in transient_types):
        return True

    # 字符串匹配兜底
    transient_markers = (
        "request exception", "connection", "timeout", "timed out",
        "network", "read timed out", "connect timed out",
        "503", "502", "504",
    )
    return any(m in err_msg for m in transient_markers)


def _is_benign_exception(e: Exception) -> bool:
    """判断异常是否为业务层面的良性错误。"""
    err_msg = str(e).lower()
    benign_markers = (
        "order not found", "not found",
        "already matched", "already matched or canceled",
        "already canceled", "already cancelled", "already cancelled or matched",
        "order is not active", "not active", "no open order", "no matching order",
        "market closed", "market resolved", "expired",
    )
    return any(m in err_msg for m in benign_markers)


def fetch_order_status(order_id: str, *, account: AccountContext, mock: bool = False, _retry: int = 0) -> dict:
    """Fetch current CLOB order status with best-effort normalization."""
    MAX_RETRIES = 3

    if not order_id:
        return {"success": False, "error": "missing_order_id"}

    if mock or order_id.startswith("DRY-"):
        return {"success": True, "order_id": order_id, "status": "submitted", "simulated": True}

    try:
        client = get_direct_clob_client(account=account)
        result = client.get_order(order_id)
        if not isinstance(result, dict):
            return {"success": False, "order_id": order_id, "error": f"unexpected_order_status_response:{result}"}

        status = str(result.get("status") or "").lower()
        from strategy.status import normalize_clob_status
        normalized_status = normalize_clob_status(status)

        size_matched = result.get("size_matched")
        created_price = result.get("price")
        average_fill_price = _extract_fill_price(result)
        filled_amount_usd = None
        try:
            if size_matched is not None and average_fill_price is not None:
                filled_amount_usd = float(size_matched) * float(average_fill_price)
        except (TypeError, ValueError):
            filled_amount_usd = None

        created_at = result.get("created_at")

        return {
            "success": True,
            "order_id": order_id,
            "status": normalized_status,
            "raw_status": status,
            "price": created_price,
            "shares": size_matched,
            "filled_shares": size_matched,
            "average_fill_price": average_fill_price,
            "filled_amount_usd": filled_amount_usd,
            "created_at": created_at,
            "raw": result,
        }
    except Exception as e:
        err_str = str(e)
        err_type = type(e).__name__

        # 业务层面的良性错误：按"已取消"处理
        if _is_benign_exception(e):
            return {"success": True, "order_id": order_id, "status": "cancelled", "note": f"benign_status:{err_str[:200]}"}

        # 临时性网络错误：指数退避重试
        if _is_transient_exception(e) and _retry < MAX_RETRIES:
            wait_sec = 0.5 * (2 ** _retry)
            # 连续 2 次异常才打印警告（第 1 次不打印，避免刷屏）
            if _retry >= 1:
                ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                print(f"   ⚠️ [{ts}] transient_error order_id={order_id} retry={_retry+1}/{MAX_RETRIES} wait={wait_sec}s: {err_type} {err_str[:120]}")
            time.sleep(wait_sec)
            return fetch_order_status(order_id, account=account, mock=mock, _retry=_retry + 1)

        # 重试耗尽或非临时错误：打印并返回错误
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        import traceback as tb
        tb_str = tb.format_exc()
        print(f"   ⚠️ [{ts}] failed order_id={order_id} after {_retry} retries: {err_type} {err_str[:150]}")
        print(f"      traceback: {tb_str[:500]}")
        return {"success": False, "order_id": order_id, "error": f"fetch_order_status_error:{err_str[:200]}"}
