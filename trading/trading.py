"""
Trading execution layer.

Handles:
- Simmer SDK import / trade / cancel
- Direct Polymarket CLOB live trading
- Position sizing (fixed or smart-sizing)
- Portfolio and position queries
"""

import os
import sys
from dataclasses import asdict

from core.constants import TRADE_SOURCE, SKILL_SLUG, SMART_SIZING_PCT, MIN_SHARES_PER_ORDER
from api import (
    get_client, should_use_direct_live_clob, get_execution_route,
    direct_polymarket_trade as _direct_polymarket_trade,
    ensure_wallet_linked_with_retry,
    _is_retryable_wallet_link_error,
)


# =============================================================================
# Simmer Trade Execution
# =============================================================================

def import_fast_market(slug):
    """Import a fast market to Simmer. Returns (market_id, error)."""
    try:
        result = get_client().import_market(f"https://polymarket.com/event/{slug}")
    except Exception as e:
        return None, str(e)

    if not result:
        return None, "No response from import endpoint"
    if result.get("error"):
        return None, result.get("error", "Unknown error")

    status = result.get("status")
    market_id = result.get("market_id")

    if status == "resolved":
        alternatives = result.get("active_alternatives", [])
        if alternatives:
            return None, f"Market resolved. Try alternative: {alternatives[0].get('id')}"
        return None, "Market resolved, no alternatives found"

    if status in ("imported", "already_exists"):
        return market_id, None

    return None, f"Unexpected status: {status}"


def get_market_details(market_id):
    """Fetch market details by ID."""
    try:
        market = get_client().get_market_by_id(market_id)
        if not market:
            return None
        return asdict(market)
    except Exception:
        return None


def get_portfolio():
    """Get portfolio summary."""
    try:
        return get_client().get_portfolio()
    except Exception as e:
        return {"error": str(e)}


def get_positions():
    """Get current positions as list of dicts, filtered by venue."""
    try:
        client = get_client()
        positions = client.get_positions(venue=client.venue)
        return [asdict(p) for p in positions]
    except Exception:
        return []


def execute_trade(market_id, side, amount, signal_data=None,
                  direct_context=None, dry_run=True):
    """
    Execute a trade via Simmer paper mode or direct Polymarket CLOB live mode.

    Args:
        market_id: Simmer market ID or slug
        side: "yes" or "no"
        amount: USD amount to spend
        signal_data: optional dict of signal metadata
        direct_context: optional dict with {price, clob_token_ids, fee_rate_bps, condition_id, slug}
        dry_run: if True, paper mode

    Returns:
        dict with success, trade_id, shares_bought, error, simulated fields
    """
    try:
        if should_use_direct_live_clob(dry_run):
            direct_context = direct_context or {}
            # For direct CLOB, use condition_id for market lookup (not Simmer's UUID market_id)
            # condition_id takes priority over slug/market_id for CLOB API calls
            condition_id = direct_context.get("condition_id")
            slug = direct_context.get("slug")
            # Use condition_id as primary key for CLOB, slug as fallback
            clob_market_key = condition_id or slug or market_id
            return _direct_polymarket_trade(
                side=side,
                amount=amount,
                price=float(direct_context.get("price") or 0),
                clob_token_ids=direct_context.get("clob_token_ids") or [],
                fee_rate_bps=direct_context.get("fee_rate_bps") or 0,
                # Pass condition_id explicitly for CLOB market lookup
                condition_id=condition_id,
                # Use slug for display if condition_id is not available
                market_id=slug or market_id,
            )

        if not dry_run and get_execution_route() == "direct_clob":
            return {
                "success": False,
                "error": "direct_clob_route_requires_wallet_private_key",
                "skip_reason": "missing_wallet_private_key",
            }

        wallet_ok, wallet_error = ensure_wallet_linked_with_retry()
        if not wallet_ok:
            return {
                "success": False,
                "error": f"wallet_link_failed: {wallet_error}",
                "skip_reason": "wallet_link_failed",
                "retryable": _is_retryable_wallet_link_error(wallet_error),
            }

        trade_kwargs = {
            "market_id": market_id,
            "side": side,
            "amount": amount,
            "order_type": _get_order_type(),
            "source": TRADE_SOURCE,
            "skill_slug": SKILL_SLUG,
        }
        if signal_data is not None:
            trade_kwargs["signal_data"] = signal_data
        try:
            result = get_client().trade(**trade_kwargs)
        except TypeError as e:
            if "signal_data" not in str(e):
                raise
            trade_kwargs.pop("signal_data", None)
            result = get_client().trade(**trade_kwargs)
        return {
            "success": result.success,
            "trade_id": result.trade_id,
            "shares_bought": result.shares_bought,
            "shares": result.shares_bought,
            "error": result.error,
            "simulated": result.simulated,
        }
    except Exception as e:
        return {"error": str(e)}


def _get_order_type():
    from config import ORDER_TYPE
    return ORDER_TYPE.upper() if ORDER_TYPE else "GTC"


# =============================================================================
# Position Sizing
# =============================================================================

def calculate_position_size(max_size, smart_sizing=False):
    """Calculate position size, optionally based on portfolio."""
    if not smart_sizing:
        return max_size
    portfolio = get_portfolio()
    if not portfolio or portfolio.get("error"):
        return max_size
    balance = portfolio.get("balance_usdc", 0)
    if balance <= 0:
        return max_size
    smart_size = balance * SMART_SIZING_PCT
    return min(smart_size, max_size)


# =============================================================================
# GTC Stale Order Cleanup
# =============================================================================

def cleanup_stale_gtc_orders(order_type, dry_run=True):
    """
    Cancel any open GTC orders from previous cycles.
    GTC orders sit on the CLOB indefinitely — if a previous cycle's order
    wasn't filled, it locks collateral and can fill unexpectedly after the
    market window has passed. Cancel them before placing new trades.
    Returns the number of cancelled orders.
    """
    from config import ORDER_TYPE
    if ORDER_TYPE != "GTC" or dry_run:
        return 0

    try:
        open_orders = get_client().get_open_orders()
        orders = open_orders.get("orders", [])
        if not orders:
            return 0

        cancelled = 0
        for order in orders:
            source = (order.get("source") or "").lower()
            slug = (order.get("skill_slug") or "").lower()
            question = (order.get("question") or "").lower()
            is_ours = (
                source == TRADE_SOURCE
                or slug == SKILL_SLUG
                or "up or down" in question
            )
            if not is_ours:
                continue
            oid = order.get("order_id") or order.get("id")
            if not oid:
                continue
            result = get_client().cancel_order(oid)
            if result.get("success"):
                cancelled += 1
                print(f"  🧹 Cancelled stale GTC order {oid[:16]}...")
            elif result.get("warning"):
                pass  # already filled — not stale

        if cancelled > 0:
            print(f"  🧹 Cleaned up {cancelled} stale GTC order(s) from previous cycles", flush=True)
        return cancelled
    except Exception as e:
        print(f"  ⚠️  GTC cleanup check failed (non-fatal): {e}")
        return 0
