"""
Market discovery for Polymarket fast markets.

Finds active BTC/ETH/SOL fast markets via:
1. Simmer SDK (primary) - markets already imported, server-computed is_live_now
2. Gamma API (fallback) - live Polymarket slot discovery

Also provides utilities for parsing end times and selecting the best market to trade.
"""

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from core.constants import ASSET_PATTERNS, WINDOW_SECONDS
from api import get_client, api_request as _api_request


# =============================================================================
# Network Health Check
# =============================================================================

def _check_network_health():
    """Check if external APIs are reachable. Returns (ok, warnings)."""
    warnings = []

    # Check if proxy is blocking external connections
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        # Try a quick request to see if proxy is working
        result = _api_request("https://api.binance.com/api/v3/ping", timeout=3)
        if result and isinstance(result, dict) and result.get("error"):
            warnings.append(f"Proxy {proxy} may be blocking external APIs: {result['error']}")

    return len(warnings) == 0, warnings


# =============================================================================
# Time Parsing Utilities
# =============================================================================

def parse_resolves_at(resolves_at_str):
    """Parse a resolves_at string (ISO format) into a timezone-aware UTC datetime."""
    try:
        s = resolves_at_str.replace("Z", "+00:00").replace(" ", "T")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def parse_fast_market_end_time(question):
    """Parse end time from fast market question (Gamma fallback path).

    e.g. 'Bitcoin Up or Down - February 15, 5:30AM-5:35AM ET' → datetime
    """
    pattern = r'(\w+ \d+),.*?-\s*(\d{1,2}:\d{2}(?:AM|PM))\s*ET'
    match = re.search(pattern, question)
    if not match:
        return None
    try:
        date_str = match.group(1)
        time_str = match.group(2)
        year = datetime.now(timezone.utc).year
        dt_str = f"{date_str} {year} {time_str}"
        dt = datetime.strptime(dt_str, "%B %d %Y %I:%M%p")
        et = ZoneInfo("America/New_York")
        dt = dt.replace(tzinfo=et).astimezone(timezone.utc)
        return dt
    except Exception:
        return None


# =============================================================================
# Simmer-based Discovery (Primary)
# =============================================================================

def discover_fast_market_markets(asset="BTC", window="5m", use_simmer=True):
    """Find active fast markets with deterministic Polymarket slots first.

    Fast markets are extremely time-sensitive, so this function avoids letting
    Simmer SDK timeouts block the current 5m window. It first queries the
    deterministic Polymarket event slug for the current slot, then uses broad
    Gamma and short-budget Simmer as supplemental sources.

    Returns markets with consistent field naming:
    - condition_id: Polymarket CLOB condition_id (64-char hex) - for CLOB API calls
    - slug: market slug - for display
    - market_id: Simmer UUID (internal use only)
    - clob_token_ids: [yes_token_id, no_token_id] - for trading
    """
    # Check network health at startup
    if use_simmer:
        ok, warnings = _check_network_health()
        for warning in warnings:
            print(f"  ⚠️  Network: {warning}")

    markets = []
    simmer_market_ids = []  # Track Simmer market IDs for condition_id lookup.

    deterministic_markets = discover_deterministic_slots(asset, window)
    markets.extend(deterministic_markets)

    has_deterministic_live = any(
        m.get("source") == "gamma_deterministic" and m.get("is_live_now")
        for m in markets
    )

    # Broad Gamma is useful as a backup, but keyset results are occasionally
    # stale. Avoid it entirely when the exact deterministic slot is live.
    if not has_deterministic_live:
        seen_tokens = {t for m in markets for t in (m.get("clob_token_ids") or [])}
        for gm in discover_via_gamma(asset, window):
            gtokens = gm.get("clob_token_ids") or []
            if gtokens and any(t in seen_tokens for t in gtokens):
                continue
            markets.append(gm)
            seen_tokens.update(gtokens)

    if use_simmer and not has_deterministic_live:
        timeout = float(os.environ.get("SIMMER_FASTLOOP_DISCOVERY_SIMMER_TIMEOUT_SEC", "1.5"))
        try:
            simmer_markets = _discover_simmer_markets_with_timeout(asset, window, timeout)
            if simmer_markets:
                seen_tokens = {t for m in markets for t in (m.get("clob_token_ids") or [])}
                for sm in simmer_markets:
                    simmer_market_ids.append(sm.get("market_id"))
                    stokens = sm.get("clob_token_ids") or []
                    if stokens and any(t in seen_tokens for t in stokens):
                        _merge_simmer_metadata(markets, sm)
                        continue
                    markets.append(sm)
                    seen_tokens.update(stokens)
        except TimeoutError:
            print(f"  ⚠️  Simmer fast-markets API timed out after {timeout:.1f}s; using Polymarket deterministic/Gamma markets")
        except Exception as e:
            print(f"  ⚠️  Simmer fast-markets API failed ({e}); using Polymarket deterministic/Gamma markets")

    # Populate condition_id and slug for Simmer markets via Gamma lookup
    # This ensures we have the correct identifiers for CLOB API calls
    if simmer_market_ids:
        _enrich_simmer_markets_with_condition_ids(markets, asset, window)

    return markets


def _discover_simmer_markets_with_timeout(asset, window, timeout_sec):
    """Fetch Simmer markets with a caller-controlled time budget.

    This is a critical time-sensitive path for fast markets. If Simmer SDK
    blocks (e.g. due to network/proxy issues), we must bail out quickly to
    avoid missing the trading window.
    """
    import signal

    def _timeout_handler(signum, frame):
        raise TimeoutError(f"Simmer SDK call exceeded {timeout_sec}s limit")

    # Set a hard signal-based timeout as a last resort
    # (ThreadPoolExecutor timeout may not catch C-level network blocks)
    old_handler = None
    if hasattr(signal, 'SIGALRM'):
        old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(max(1, int(timeout_sec)))

    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_discover_simmer_markets, asset, window)
    try:
        return future.result(timeout=max(0.1, float(timeout_sec)))
    except TimeoutError:
        print(f"  ⚠️  Simmer SDK timed out after {timeout_sec}s; will use Polymarket API only")
        raise
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
        if hasattr(signal, 'SIGALRM') and old_handler is not None:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)


def _discover_simmer_markets(asset, window):
    client = get_client()
    sdk_markets = client.get_fast_markets(asset=asset, window=window, limit=50)
    markets = []
    for m in sdk_markets or []:
        end_time = parse_resolves_at(m.resolves_at) if m.resolves_at else None
        clob_tokens = [m.polymarket_token_id] if m.polymarket_token_id else []
        if m.polymarket_no_token_id:
            clob_tokens.append(m.polymarket_no_token_id)
        markets.append({
            "question": m.question,
            "market_id": m.id,  # Simmer UUID - for internal use only
            "condition_id": None,  # Will be populated via Gamma lookup below
            "slug": None,  # Will be populated via Gamma lookup below
            "end_time": end_time,
            "clob_token_ids": clob_tokens,
            "is_live_now": m.is_live_now,
            "spread_cents": m.spread_cents,
            "liquidity_tier": m.liquidity_tier,
            "external_price_yes": m.external_price_yes,
            "fee_rate_bps": getattr(m, 'fee_rate_bps', 0),
            "source": "simmer",
        })
    return markets


def _merge_simmer_metadata(markets, simmer_market):
    """Copy Simmer-only display/liquidity fields onto an existing token match."""
    stokens = {str(t) for t in (simmer_market.get("clob_token_ids") or []) if t}
    if not stokens:
        return
    for market in markets:
        mtokens = {str(t) for t in (market.get("clob_token_ids") or []) if t}
        if not (stokens & mtokens):
            continue
        for key in ("market_id", "spread_cents", "liquidity_tier", "external_price_yes"):
            if simmer_market.get(key) is not None and market.get(key) is None:
                market[key] = simmer_market[key]
        if simmer_market.get("fee_rate_bps") and not market.get("fee_rate_bps"):
            market["fee_rate_bps"] = simmer_market["fee_rate_bps"]
        market["simmer_matched"] = True
        return


# =============================================================================
# Gamma API Discovery (Fallback)
# =============================================================================

def discover_via_gamma(asset="BTC", window="5m"):
    """Fallback: Find active fast markets on Polymarket via Gamma API."""
    patterns = ASSET_PATTERNS.get(asset, ASSET_PATTERNS["BTC"])
    url = (
        "https://gamma-api.polymarket.com/markets/keyset"
        "?limit=100&closed=false&tag=crypto&order=endDate&ascending=true"
    )
    result = _api_request(url)
    if not result or not isinstance(result, dict) or result.get("error"):
        return []
    raw_markets = result.get("markets", [])

    markets = []
    for m in raw_markets:
        q = (m.get("question") or "").lower()
        slug = m.get("slug", "")
        matches_window = f"-{window}-" in slug
        if any(p in q for p in patterns) and matches_window:
            condition_id = m.get("conditionId", "")
            closed = m.get("closed", False)
            if not closed and slug:
                end_time = parse_fast_market_end_time(m.get("question", ""))
                clob_tokens_raw = m.get("clobTokenIds", "[]")
                if isinstance(clob_tokens_raw, str):
                    try:
                        clob_tokens = json.loads(clob_tokens_raw)
                    except (json.JSONDecodeError, ValueError):
                        clob_tokens = []
                else:
                    clob_tokens = clob_tokens_raw or []
                markets.append({
                    "question": m.get("question", ""),
                    "slug": slug,
                    "condition_id": condition_id,
                    "end_time": end_time,
                    "outcomes": m.get("outcomes", []),
                    "outcome_prices": m.get("outcomePrices", "[]"),
                    "clob_token_ids": clob_tokens,
                    "fee_rate_bps": int(m.get("fee_rate_bps") or m.get("feeRateBps") or 0),
                    "source": "gamma",
                })
    return markets


def discover_deterministic_slots(asset="BTC", window="5m"):
    """Find fast markets by deterministic slug timestamp.

    Polymarket crypto fast-market event slugs use the current UTC slot start,
    e.g. btc-updown-5m-1778397900. This path is more reliable than broad Gamma
    searches when the index returns stale or far-future rows first.

    We query current_slot ± 3 slots (15 minutes range) to catch pre-open markets
    that are within the lead_time window but whose start_time hasn't arrived yet.
    """
    asset = (asset or "BTC").upper()
    prefix = {
        "BTC": "btc",
        "ETH": "eth",
        "SOL": "sol",
    }.get(asset)
    if not prefix:
        return []

    now = datetime.now(timezone.utc)
    window_seconds = WINDOW_SECONDS.get(window, 300)
    current_slot = int(now.timestamp() // window_seconds * window_seconds)
    timeout = float(os.environ.get("SIMMER_FASTLOOP_DISCOVERY_GAMMA_TIMEOUT_SEC", "1.5"))

    markets = []
    # Query ± 3 slots (15 minutes range) to find pre-open markets within lead_time
    for slot in (
        current_slot - 3 * window_seconds,
        current_slot - 2 * window_seconds,
        current_slot - window_seconds,
        current_slot,
        current_slot + window_seconds,
        current_slot + 2 * window_seconds,
        current_slot + 3 * window_seconds,
    ):
        slug = f"{prefix}-updown-{window}-{slot}"
        event = _api_request(f"https://gamma-api.polymarket.com/events/slug/{slug}", timeout=timeout)
        if not event or not isinstance(event, dict) or event.get("error"):
            continue
        for m in event.get("markets") or []:
            if m.get("closed"):
                continue
            clob_tokens_raw = m.get("clobTokenIds", "[]")
            if isinstance(clob_tokens_raw, str):
                try:
                    clob_tokens = json.loads(clob_tokens_raw)
                except (json.JSONDecodeError, ValueError):
                    clob_tokens = []
            else:
                clob_tokens = clob_tokens_raw or []
            end_time = parse_resolves_at(m.get("endDate") or event.get("endDate") or "")
            if not end_time:
                continue
            remaining = (end_time - now).total_seconds()
            is_live_now = (
                bool(event.get("active", True))
                and bool(m.get("active", True))
                and bool(m.get("acceptingOrders", True))
                and 0 < remaining <= window_seconds
            )
            spread_cents = None
            try:
                spread_cents = float(m.get("spread")) * 100
            except (TypeError, ValueError):
                pass
            markets.append({
                "question": m.get("question") or event.get("title") or "",
                "slug": m.get("slug") or slug,
                "condition_id": m.get("conditionId", ""),
                "end_time": end_time,
                "outcomes": m.get("outcomes", []),
                "outcome_prices": m.get("outcomePrices", "[]"),
                "clob_token_ids": clob_tokens,
                "fee_rate_bps": int(m.get("fee_rate_bps") or m.get("feeRateBps") or 0),
                "is_live_now": is_live_now,
                "spread_cents": spread_cents,
                "liquidity_tier": "deterministic",
                "source": "gamma_deterministic",
            })
    return markets


# =============================================================================
# Market Selection
# =============================================================================

def _enrich_simmer_markets_with_condition_ids(markets, asset, window):
    """Populate condition_id and slug for Simmer markets by looking up via Gamma API.

    Simmer returns market_id (UUID) but not condition_id (Polymarket CLOB identifier).
    We use the clob_token_ids to match Simmer markets with Gamma data.

    This ensures direct CLOB trading uses the correct condition_id.
    """
    if not markets:
        return

    # Get all clob_token_ids from Simmer markets
    simmer_tokens = {}
    for m in markets:
        if m.get("source") == "simmer" and m.get("clob_token_ids"):
            for token_id in m["clob_token_ids"]:
                if token_id:
                    simmer_tokens[str(token_id)] = m

    if not simmer_tokens:
        return

    # Fetch Gamma markets to find matching condition_ids
    patterns = ASSET_PATTERNS.get(asset, ASSET_PATTERNS["BTC"])
    url = (
        "https://gamma-api.polymarket.com/markets/keyset"
        "?limit=200&closed=false&tag=crypto&order=endDate&ascending=true"
    )
    result = _api_request(url)
    if not result or not isinstance(result, dict):
        return

    raw_markets = result.get("markets", [])
    enriched_count = 0

    for gm in raw_markets:
        q = (gm.get("question") or "").lower()
        slug = gm.get("slug", "")
        matches_window = f"-{window}-" in slug

        if not (any(p in q for p in patterns) and matches_window):
            continue

        condition_id = gm.get("conditionId", "")
        clob_tokens_raw = gm.get("clobTokenIds", "[]")
        if isinstance(clob_tokens_raw, str):
            try:
                clob_tokens = json.loads(clob_tokens_raw)
            except (json.JSONDecodeError, ValueError):
                clob_tokens = []
        else:
            clob_tokens = clob_tokens_raw or []

        # Match by clob_token_id
        for token_id in clob_tokens:
            if str(token_id) in simmer_tokens:
                simmer_market = simmer_tokens[str(token_id)]
                if not simmer_market.get("condition_id") and condition_id:
                    simmer_market["condition_id"] = condition_id
                    simmer_market["slug"] = slug
                    enriched_count += 1
                    break

    if enriched_count > 0:
        print(f"  ℹ️  Enriched {enriched_count} Simmer markets with condition_id from Gamma")


def find_best_fast_market(markets, window="5m", min_time_remaining=0):
    """Pick the best fast_market to trade: live now, soonest expiring, enough time remaining."""
    now = datetime.now(timezone.utc)
    max_remaining = WINDOW_SECONDS.get(window, 300) * 2
    candidates = []
    for m in markets:
        if m.get("is_live_now") is not None:
            if not m["is_live_now"]:
                continue
            end_time = m.get("end_time")
            if end_time:
                remaining = (end_time - now).total_seconds()
                if remaining > min_time_remaining:
                    candidates.append((remaining, m))
        else:
            end_time = m.get("end_time")
            if not end_time:
                continue
            remaining = (end_time - now).total_seconds()
            if remaining > min_time_remaining and remaining < max_remaining:
                candidates.append((remaining, m))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]
