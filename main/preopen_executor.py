"""
Pre-open executor: YES entry + Down hedge placement and state machine.

Orchestrates:
  1. Place YES limit order (shares × price <= yes_max_price)
  2. Place Down GTC PostOnly @0.40
  3. Near open (ttl <= 40s): switch Down to FAK <= 0.42 if arb edge survives
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from api import fetch_side_orderbook_price, direct_polymarket_trade, cancel_order
from core import (
    PREOPEN_YES_SHARES_X, PREOPEN_YES_MAX_PRICE,
    PREOPEN_HEDGE_RATIO, PREOPEN_DOWN_RESTING_PRICE,
    PREOPEN_DOWN_SWITCH_TTL_SEC, PREOPEN_DOWN_FAK_MAX_PRICE,
    PREOPEN_MIN_ARB_EDGE, PREOPEN_MAX_ACTIONS_PER_EVENT,
)

from .preopen_event_pool import PreopenEvent, PreopenEventPool, EventState
from .preopen_arb import check_arb_from_orderbook, evaluate_arb_edge


class ActionResult(Enum):
    SKIPPED = "skipped"
    PLACED = "placed"
    CANCELLED = "cancelled"
    REPLACED = "replaced"
    ERROR = "error"


@dataclass
class ExecutorAction:
    action: str           # "yes_entry", "down_resting", "down_switch"
    result: ActionResult
    order_id: str | None
    price: float | None
    shares: float | None
    error: str | None = None


def _place_yes_order(
    event: PreopenEvent,
    dry_run: bool,
) -> ExecutorAction:
    """Place YES limit order: buy yes_shares_x shares at limit <= yes_max_price."""
    # Fetch current YES ask to check price constraint
    side_book = fetch_side_orderbook_price(event.clob_token_ids, "yes")
    current_yes_ask = side_book.get("best_ask") if side_book else None

    if current_yes_ask is None:
        return ExecutorAction(
            action="yes_entry",
            result=ActionResult.ERROR,
            order_id=None,
            price=None,
            shares=None,
            error="cannot_fetch_yes_ask",
        )

    if current_yes_ask > PREOPEN_YES_MAX_PRICE:
        return ExecutorAction(
            action="yes_entry",
            result=ActionResult.SKIPPED,
            order_id=None,
            price=float(current_yes_ask),
            shares=None,
            error=f"yes_ask={current_yes_ask:.4f} > max_price={PREOPEN_YES_MAX_PRICE}",
        )

    shares = PREOPEN_YES_SHARES_X
    limit_price = min(float(current_yes_ask), PREOPEN_YES_MAX_PRICE)

    if dry_run:
        return ExecutorAction(
            action="yes_entry",
            result=ActionResult.PLACED,
            order_id=f"DRY-{event.condition_id[:8]}-YES",
            price=limit_price,
            shares=shares,
            error=None,
        )

    # Live execution: place GTC limit order for YES
    result = direct_polymarket_trade(
        side="yes",
        amount=shares * limit_price,  # USD amount
        price=limit_price,
        clob_token_ids=event.clob_token_ids,
        fee_rate_bps=event.fee_rate_bps,
        condition_id=event.condition_id,
        order_type_override="GTC",
    )

    if result.get("success"):
        order_id = result.get("trade_id")
        # Use actual fill price from CLOB response; fall back to limit price
        fill_price = result.get("fill_price")
        if fill_price is None:
            fill_price = limit_price
        return ExecutorAction(
            action="yes_entry",
            result=ActionResult.PLACED,
            order_id=order_id,
            price=fill_price,
            shares=shares,
            error=None,
        )
    else:
        return ExecutorAction(
            action="yes_entry",
            result=ActionResult.ERROR,
            order_id=None,
            price=limit_price,
            shares=shares,
            error=result.get("error", "unknown"),
        )


def _place_down_resting_order(
    event: PreopenEvent,
    dry_run: bool,
) -> ExecutorAction:
    """Place Down (NO) GTC PostOnly @ preopen_down_resting_price."""
    if len(event.clob_token_ids) < 2:
        return ExecutorAction(
            action="down_resting",
            result=ActionResult.ERROR,
            order_id=None,
            price=PREOPEN_DOWN_RESTING_PRICE,
            shares=None,
            error="missing_no_token",
        )

    down_shares = PREOPEN_YES_SHARES_X * PREOPEN_HEDGE_RATIO
    rest_price = PREOPEN_DOWN_RESTING_PRICE

    if dry_run:
        return ExecutorAction(
            action="down_resting",
            result=ActionResult.PLACED,
            order_id=f"DRY-{event.condition_id[:8]}-DOWN",
            price=rest_price,
            shares=down_shares,
            error=None,
        )

    # Live: place GTC PostOnly for NO (Down hedge)
    # PostOnly ensures we don't cross the spread
    result = direct_polymarket_trade(
        side="no",
        amount=down_shares * rest_price,
        price=rest_price,
        clob_token_ids=event.clob_token_ids,
        fee_rate_bps=event.fee_rate_bps,
        condition_id=event.condition_id,
        order_type_override="GTC",
        post_only=True,
    )

    if result.get("success"):
        fill_price = result.get("fill_price")
        if fill_price is None:
            fill_price = rest_price
        event.down_fill_price = fill_price
        return ExecutorAction(
            action="down_resting",
            result=ActionResult.PLACED,
            order_id=result.get("trade_id"),
            price=fill_price,
            shares=down_shares,
            error=None,
        )
    else:
        return ExecutorAction(
            action="down_resting",
            result=ActionResult.ERROR,
            order_id=None,
            price=rest_price,
            shares=down_shares,
            error=result.get("error", "unknown"),
        )


def _switch_down_to_fak(
    event: PreopenEvent,
    dry_run: bool,
    fetch_side_orderbook_price_fn,
) -> ExecutorAction:
    """
    Switch Down from GTC Resting to FAK <= down_fak_max_price.
    First checks arbitrage edge; if insufficient, keeps resting order.
    """
    if event.yes_fill_price is None:
        return ExecutorAction(
            action="down_switch",
            result=ActionResult.SKIPPED,
            order_id=None,
            price=None,
            shares=None,
            error="no_yes_fill_price",
        )

    # Evaluate arbitrage edge
    arb_result = check_arb_from_orderbook(
        yes_fill_price=event.yes_fill_price,
        clob_token_ids=event.clob_token_ids,
        fee_rate_bps=event.fee_rate_bps,
        min_arb_edge=PREOPEN_MIN_ARB_EDGE,
        fetch_side_orderbook_price=fetch_side_orderbook_price_fn,
    )

    if arb_result is None:
        return ExecutorAction(
            action="down_switch",
            result=ActionResult.SKIPPED,
            order_id=None,
            price=None,
            shares=None,
            error="cannot_evaluate_arb",
        )

    if not arb_result.sufficient:
        return ExecutorAction(
            action="down_switch",
            result=ActionResult.SKIPPED,
            order_id=None,
            price=arb_result.down_executable_price,
            shares=None,
            error=f"arb_insufficient: {arb_result.reason}",
        )

    # Arb edge is sufficient — switch to FAK
    # In production: cancel resting order, then place FAK <= down_fak_max_price
    # For now: simulate by placing FAK order
    down_shares = PREOPEN_YES_SHARES_X * PREOPEN_HEDGE_RATIO
    fak_price = PREOPEN_DOWN_FAK_MAX_PRICE

    if dry_run:
        return ExecutorAction(
            action="down_switch",
            result=ActionResult.REPLACED,
            order_id=f"DRY-{event.condition_id[:8]}-DOWN-FAK",
            price=fak_price,
            shares=down_shares,
            error=None,
        )

    # Cancel existing resting order
    if event.down_order_id:
        cancel_order(event.down_order_id)

    # Place FAK order for NO (Down hedge)
    result = direct_polymarket_trade(
        side="no",
        amount=down_shares * fak_price,
        price=fak_price,
        clob_token_ids=event.clob_token_ids,
        fee_rate_bps=event.fee_rate_bps,
        condition_id=event.condition_id,
        order_type_override="FAK",
    )

    if result.get("success"):
        fill_price = result.get("fill_price")
        if fill_price is None:
            fill_price = fak_price
        return ExecutorAction(
            action="down_switch",
            result=ActionResult.REPLACED,
            order_id=result.get("trade_id"),
            price=fill_price,
            shares=down_shares,
            error=None,
        )
    else:
        return ExecutorAction(
            action="down_switch",
            result=ActionResult.ERROR,
            order_id=None,
            price=fak_price,
            shares=down_shares,
            error=result.get("error", "unknown"),
        )


def execute_event_cycle(
    event: PreopenEvent,
    pool: PreopenEventPool,
    dry_run: bool,
    now: datetime | None = None,
) -> list[ExecutorAction]:
    """
    Execute one full pre-open cycle for an event.

    State machine:
      READY → YES_PLACED → DOWN_RESTING → (DOWN_SWITCHED if ttl <= 40s & arb survives)

    Returns list of ExecutorAction taken.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    actions: list[ExecutorAction] = []

    # ── Action limit guard ───────────────────────────────────────────────────
    if event.action_count >= PREOPEN_MAX_ACTIONS_PER_EVENT:
        return [ExecutorAction(
            action="cycle",
            result=ActionResult.SKIPPED,
            order_id=None, price=None, shares=None,
            error=f"action_count={event.action_count} >= max={PREOPEN_MAX_ACTIONS_PER_EVENT}",
        )]

    # ── Phase 0: DISCOVERED → READY (automatic transition) ─────────────────
    if event.state == EventState.DISCOVERED:
        event.transition_to(EventState.READY)
        pool.add(event)
        print(f"  🔄 事件状态: DISCOVERED → READY | {event.slug[:40]}")

    # ── Phase 1: YES entry (only from READY) ────────────────────────────────
    if event.state == EventState.READY:
        action = _place_yes_order(event, dry_run)
        actions.append(action)
        if action.result == ActionResult.PLACED:
            event.yes_order_id = action.order_id
            event.yes_fill_price = action.price
            event.transition_to(EventState.YES_PLACED)
            pool.add(event)

    # ── Phase 2: Down resting order (only from YES_PLACED) ─────────────────
    if event.state == EventState.YES_PLACED:
        action = _place_down_resting_order(event, dry_run)
        actions.append(action)
        if action.result == ActionResult.PLACED:
            # down_fill_price already set inside _place_down_resting_order
            event.down_order_id = action.order_id
            event.transition_to(EventState.DOWN_RESTING)
            pool.add(event)

    # ── Phase 3: Switch to FAK near open ───────────────────────────────────
    tts = event.time_to_start(now)
    if (
        event.state == EventState.DOWN_RESTING
        and tts <= PREOPEN_DOWN_SWITCH_TTL_SEC
    ):
        action = _switch_down_to_fak(event, dry_run, fetch_side_orderbook_price)
        actions.append(action)
        if action.result == ActionResult.REPLACED:
            # down_fill_price already set inside _switch_down_to_fak
            event.down_order_id = action.order_id
            event.transition_to(EventState.DOWN_SWITCHED)
            pool.add(event)

    return actions
