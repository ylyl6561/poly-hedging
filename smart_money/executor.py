"""Execute follow-list orders against Polymarket CLOB.

Behavior
--------
* By default runs in **dry-run** mode. It builds the order object, logs it,
  writes a ``FollowOrder`` row with ``status='dry_run'`` and returns.
* When ``settings.live_trade=True`` it submits a real limit order via the
  Polymarket CLOB SDK. The CLOB client is constructed lazily; if the SDK
  isn't installed the executor fails loudly and the order stays
  ``dry_run`` / ``error``.

The executor NEVER touches state outside of:
  - ``smart_money_follow_orders`` (audit log)
  - the live CLOB API (when enabled)

It does NOT auto-resubmit. Every signal yields at most one order attempt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from api import (
    OrderEvent,
    OrderEventBus,
    OrderStatus,
    get_order_event_bus,
    new_order_id,
)

from .config import SmartMoneySettings
from .models import FollowOrder, Signal

logger = logging.getLogger(__name__)


# Phase 2D: every copy-trade attempt is mirrored onto the in-process
# OrderEventBus so the dashboard / notifier can surface it in real time.
# The executor imports ``get_order_event_bus`` lazily at call time to keep
# import-time side effects low and to keep tests cheap.
def _publish_event(
    bus: OrderEventBus | None,
    *,
    order_id: str,
    leader_wallet: str,
    market_id: str | None,
    asset_id: str | None,
    side: str | None,
    status: OrderStatus,
    reason: str | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    if bus is None:
        return
    try:
        bus.publish(
            OrderEvent(
                event_id="",
                order_id=order_id,
                leader_wallet=leader_wallet,
                market_id=market_id,
                asset_id=asset_id,
                side=side,
                status=status,
                reason=reason,
                data=data or {},
            )
        )
    except Exception:  # pragma: no cover — defensive
        logger.exception("failed to publish order event %s/%s", order_id, status)


@dataclass
class OrderPlan:
    signal_id: int
    wallet: str
    condition_id: str
    direction: str
    token_id: str | None
    side: str
    price: float
    size_usdc: float
    size_shares: float


def _build_plan(signal: Signal, settings: SmartMoneySettings, session: Session) -> OrderPlan | None:
    """Construct an order plan from a Signal row. Returns None if not actionable."""
    price = float(signal.avg_entry_price or signal.current_price or 0)
    if price <= 0 or price >= 1:
        return None
    if signal.signal_type == "consensus" and signal.trader_count < settings.follow_min_consensus_for_execute:
        return None
    if signal.signal_type == "new_open" and not settings.follow_min_consensus_for_execute <= 1:
        # user requirement: only consensus >=3 goes auto. Block new_open by default.
        return None
    suggested = float(signal.suggested_size_usdc or settings.default_position_size_usdc)
    size = max(0.0, min(suggested, settings.follow_max_size_usdc))

    # Live-trade safety caps.  These only kick in when settings.live_trade
    # is True; in dry-run mode we keep the suggested size so the simulator
    # exercises the same signal at the same nominal amount.
    if settings.live_trade:
        from datetime import datetime, timezone
        from sqlalchemy import func

        size = max(settings.live_min_size_usdc, min(size, settings.live_default_size_usdc))

        # Daily total cap.  Sum USDC across all LIVE orders whose
        # created_at falls in the current UTC day; reject if adding
        # ``size`` would exceed the cap.  Dry-run rows are excluded
        # because they never committed real capital.
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        from .models import FollowOrder as _FollowOrder
        already_deployed_today = session.execute(
            select(func.coalesce(func.sum(_FollowOrder.size_usdc), 0.0))
            .where(
                _FollowOrder.status.in_(["submitted", "filled"]),
                _FollowOrder.created_at >= today_start,
            )
        ).scalar_one()
        remaining_today = max(0.0, settings.live_max_daily_usdc - float(already_deployed_today))
        if remaining_today <= 0:
            logger.info(
                "executor live cap: daily total %s already deployed, skipping signal %s",
                already_deployed_today, signal.id,
            )
            return None
        size = min(size, remaining_today)
        logger.info(
            "executor live cap: daily remaining=%s after deploy=%s, signal %s will use size=%s",
            remaining_today, already_deployed_today, signal.id, size,
        )

        # Per-condition concurrency cap.  Only count live (not
        # dry-run) orders so historical simulated trades don't
        # block new real ones.
        from .models import FollowOrder as _FO2
        open_per_cond = session.execute(
            select(func.count(_FO2.id))
            .where(
                _FO2.condition_id == signal.condition_id,
                _FO2.status.in_(["submitted", "filled"]),
            )
        ).scalar_one()
        if int(open_per_cond) >= settings.live_max_concurrent_per_condition:
            logger.info(
                "executor live cap: condition %s already has %s open orders, skipping signal %s",
                signal.condition_id, open_per_cond, signal.id,
            )
            return None

    # Resolve token_id + trigger wallet from the underlying Trade row(s).
    token_id: str | None = None
    trigger_wallet = ""
    if signal.trigger_trade_fingerprint:
        from .models import Trade
        trade = session.execute(
            select(Trade).where(Trade.fingerprint == signal.trigger_trade_fingerprint)
        ).scalar_one_or_none()
        if trade:
            token_id = trade.token_id
            trigger_wallet = trade.wallet
    if not trigger_wallet:
        trigger_wallet = ((signal.trigger_wallets or [{}])[0]).get("wallet", "")

    # Round price to the market's tick size.  Polymarket CLOB rejects
    # any price with more precision than the market's tick (e.g. 0.295
    # for a 0.01 tick_size market).  We round to 2 decimal places by
    # default; markets with finer ticks (0.001 / 0.0001) are rare in
    # the consensus-signal universe and we trade them at our own risk.
    rounded_price = round(price, 2)
    if rounded_price <= 0 or rounded_price >= 1:
        return None

    return OrderPlan(
        signal_id=signal.id,
        wallet=trigger_wallet,
        condition_id=signal.condition_id,
        direction=signal.direction,
        token_id=token_id,
        side="BUY",
        price=rounded_price,
        size_usdc=round(size, 2),
        size_shares=round(size / rounded_price, 2) if rounded_price else 0.0,
    )


def _submit_live(plan: OrderPlan, settings: SmartMoneySettings) -> tuple[bool, str]:
    """Submit a real order via the official polymarket-client SDK.

    Uses ``polymarket-client`` (>= 0.2.0) — the new official SDK that
    wraps the deposit-wallet / POLY_1271 flow correctly.  The legacy
    ``py_clob_client_v2`` has a known bug where L1 auth binds the API
    key to the EOA instead of the deposit wallet, so every order is
    rejected with "maker address not allowed, please use the deposit
    wallet flow".  The new SDK derives the deposit wallet from the
    signer and produces an EIP-1271 wrapped signature that the V2
    exchange accepts.

    Returns (success, message). Failures are non-fatal — caller records
    the result in the audit log.
    """
    try:
        from polymarket import SecureClient  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return False, f"polymarket-client not installed: {exc!r}"

    if not plan.token_id:
        return False, "missing token_id, cannot place live order"

    if not settings.polymarket_private_key:
        return False, (
            "polymarket credentials missing: set "
            "SMART_MONEY_POLYMARKET_PRIVATE_KEY"
        )

    try:
        client = SecureClient.create(private_key=settings.polymarket_private_key)
        wallet = client.wallet
        logger.info(
            "executor live init: wallet=%s wallet_type=%s",
            wallet, client.wallet_type,
        )

        # Choose maker size.  ``plan.size_shares`` is what the SDK
        # expects (number of outcome tokens, not USDC).  Fail soft
        # if the SDK rounds the price away from the market tick.
        resp = client.place_limit_order(
            token_id=plan.token_id,
            side='BUY',
            price=str(plan.price),
            size=str(plan.size_shares),
        )

        # ``place_limit_order`` returns either ``AcceptedOrder`` (ok=True)
        # or ``RejectedOrder`` (ok=False).  Both expose the same shape.
        if getattr(resp, "ok", False):
            order_id = getattr(resp, "order_id", None) or "<unknown>"
            msg = f"order_id={order_id} status={getattr(resp, 'status', '?')}"
            return True, msg
        reason = getattr(resp, "message", None) or getattr(resp, "errorMsg", None)
        code = getattr(resp, "code", None)
        return False, f"rejected by CLOB: code={code} message={reason}"
    except Exception as exc:  # noqa: BLE001
        return False, f"clob submit failed: {exc!r}"


def execute(
    session: Session,
    settings: SmartMoneySettings,
    *,
    signal_id: int,
    bus: OrderEventBus | None = None,
) -> dict[str, Any]:
    """Process one approved signal.

    Writes a ``FollowOrder`` audit row and returns counters.

    If ``bus`` is provided, every state transition is also published as
    an :class:`~api.OrderEvent` so the dashboard / notifier can react
    in real time.  Callers that don't care can simply omit it.
    """
    if bus is None:
        # Default to the process-wide bus so dashboard / notifier code
        # always sees the same stream of events without explicit wiring.
        bus = get_order_event_bus()

    sig = session.execute(
        select(Signal).where(Signal.id == signal_id)
    ).scalar_one_or_none()
    if sig is None:
        return {"status": "missing", "signal_id": signal_id}

    if settings.live_trade and not (
        settings.polymarket_private_key and settings.polymarket_funder
    ):
        # Fail loudly.  Without credentials ``_submit_live`` would return
        # an error string, but we want a separate audit row so the
        # dashboard can highlight "live trade requested but wallet not
        # configured" without confusing it with a real CLOB rejection.
        err = (
            "live_trade enabled but SMART_MONEY_POLYMARKET_PRIVATE_KEY / "
            "SMART_MONEY_POLYMARKET_FUNDER are empty"
        )
        logger.error("executor: %s", err)
        order_id = new_order_id("", sig.condition_id)
        session.add(
            FollowOrder(
                signal_id=signal_id,
                wallet="",
                condition_id=sig.condition_id,
                direction=sig.direction,
                side="BUY",
                price=0,
                size_usdc=0,
                status="error",
                note=err,
            )
        )
        _publish_event(
            bus,
            order_id=order_id,
            leader_wallet="",
            market_id=sig.condition_id,
            asset_id=None,
            side=None,
            status=OrderStatus.FAILED,
            reason=err,
        )
        return {"status": "error", "signal_id": signal_id, "reason": err}

    plan = _build_plan(sig, settings, session)
    if plan is None:
        session.add(
            FollowOrder(
                signal_id=signal_id,
                wallet="",
                condition_id=sig.condition_id,
                direction=sig.direction,
                side="BUY",
                price=0,
                size_usdc=0,
                status="skipped",
                note="plan-build rejected (price band / consensus / no token)",
            )
        )
        skipped_id = new_order_id("", sig.condition_id)
        _publish_event(
            bus,
            order_id=skipped_id,
            leader_wallet="",
            market_id=sig.condition_id,
            asset_id=None,
            side=None,
            status=OrderStatus.SKIPPED,
            reason="plan-build rejected (price band / consensus / no token)",
            data={"signal_id": signal_id, "direction": sig.direction},
        )
        return {"status": "skipped", "signal_id": signal_id, "reason": "plan-build"}

    # Phase 2D: a stable id is generated up-front so every subsequent
    # state transition for the same copy-trade can be correlated in
    # the dashboard, the audit log, and the notifier.
    order_id = new_order_id(plan.wallet, plan.condition_id)

    _publish_event(
        bus,
        order_id=order_id,
        leader_wallet=plan.wallet,
        market_id=plan.condition_id,
        asset_id=plan.token_id,
        side=plan.side,
        status=OrderStatus.PENDING,
        reason="signal approved, queuing",
        data={
            "signal_id": signal_id,
            "price": plan.price,
            "size_usdc": plan.size_usdc,
            "size_shares": plan.size_shares,
            "direction": plan.direction,
        },
    )

    note = ""
    status = "dry_run"
    # Three execution modes:
    #   - live_trade=False                → status='dry_run' (no CLOB call)
    #   - live_trade=True, semi_auto=True → status='pending'  (audit row
    #                                          written, but no CLOB call;
    #                                          a human approves via
    #                                          /orders/{id}/approve)
    #   - live_trade=True, semi_auto=False→ CLOB call immediately
    if settings.live_trade and not settings.semi_auto:
        _publish_event(
            bus,
            order_id=order_id,
            leader_wallet=plan.wallet,
            market_id=plan.condition_id,
            asset_id=plan.token_id,
            side=plan.side,
            status=OrderStatus.INFLIGHT,
            reason="submitting to CLOB",
        )
        ok, msg = _submit_live(plan, settings)
        status = "submitted" if ok else "error"
        note = msg
        _publish_event(
            bus,
            order_id=order_id,
            leader_wallet=plan.wallet,
            market_id=plan.condition_id,
            asset_id=plan.token_id,
            side=plan.side,
            status=OrderStatus.FILLED if ok else OrderStatus.FAILED,
            reason=msg if not ok else None,
            data={"clob_message": msg},
        )
    elif settings.live_trade and settings.semi_auto:
        status = "pending"
        note = (
            f"SEMI-AUTO approval required: side={plan.side} price={plan.price} "
            f"size=${plan.size_usdc} ({plan.size_shares} shares) token_id={plan.token_id}"
        )
        logger.info("executor SEMI-AUTO pending %s", note)
        # ``source: "operator"`` because the worker is asking the
        # operator to *make* a decision — the next event (approved /
        # cancelled) is also operator-driven, so the dashboard's
        # "operator-only" live stream should include this row so the
        # operator sees the pending plan and stamps the approve button.
        _publish_event(
            bus,
            order_id=order_id,
            leader_wallet=plan.wallet,
            market_id=plan.condition_id,
            asset_id=plan.token_id,
            side=plan.side,
            status=OrderStatus.PENDING,
            reason="awaiting human approval",
            data={
                "approve_endpoint": f"/orders/{order_id}/approve",
                "source": "operator",
                "follow_order_db_id": None,  # set below after the row flush
            },
        )
    else:
        note = (
            f"DRY-RUN side={plan.side} price={plan.price} size=${plan.size_usdc} "
            f"({plan.size_shares} shares) token_id={plan.token_id}"
        )
        logger.info("executor DRY-RUN %s", note)
        _publish_event(
            bus,
            order_id=order_id,
            leader_wallet=plan.wallet,
            market_id=plan.condition_id,
            asset_id=plan.token_id,
            side=plan.side,
            status=OrderStatus.MIRRORED,
            reason="dry-run simulated",
            data={"note": note},
        )

    # Flush so the cascade-generated FollowOrder.id is available for the
    # downstream ``OrderEvent`` payload.  We want the dashboard's "Approve"
    # button on the live event row to point at the same DB row so an
    # operator can stamp the half-built plan into a real CLOB order in one
    # click.
    new_order = FollowOrder(
        signal_id=signal_id,
        wallet=plan.wallet,
        condition_id=plan.condition_id,
        direction=plan.direction,
        token_id=plan.token_id,
        side=plan.side,
        price=plan.price,
        size_usdc=plan.size_usdc,
        status=status,
        note=note,
    )
    session.add(new_order)
    session.flush()

    # Publish a final trailing event so the dashboard can render an
    # "Approve" button when ``status='pending'``.  We re-use the same
    # order_id so existing renderer rows update in place via the SSE
    # de-duplication map.
    _publish_event(
        bus,
        order_id=order_id,
        leader_wallet=plan.wallet,
        market_id=plan.condition_id,
        asset_id=plan.token_id,
        side=plan.side,
        status=OrderStatus.PENDING if status == "pending" else (
            OrderStatus.INFLIGHT if status == "submitted" else OrderStatus.SKIPPED
        ),
        reason=note,
        data={
            "follow_order_db_id": new_order.id,
            "price": plan.price,
            "size_usdc": plan.size_usdc,
            "size_shares": plan.size_shares,
            "direction": plan.direction,
            # Pending SEMI-AUTO plans are an operator-decision row;
            # the dashboard's "operator-only" filter should surface
            # them so the operator can stamp the approve button.
            "source": "operator" if status == "pending" else None,
        },
    )
    return {
        "status": status,
        "signal_id": signal_id,
        "order_id": order_id,
        "plan": plan.__dict__,
        "note": note,
    }