"""Manual order + leader-sales detection for the Phase 4 dashboard.

This module sits alongside ``executor.py`` and is intentionally narrowly
scoped: it provides two operations the dashboard's *Trading* tab needs:

* ``submit_manual_order`` — place (or dry-run) a single mirror order,
  independent of any signal.  Used when the operator wants to follow a
  leader *right now* from the dashboard rather than via the consensus
  signal pipeline.

* ``detect_leader_sales`` — scan the ``Trade`` table for follow-list
  wallets that just SELL'd on a market where we currently hold a
  position (via ``FollowOrder``).  Emits OrderEvents and pushes a Feishu
  alert so the operator is told that the leader has exited even when
  the bot itself didn't act.

Both operations publish onto the process-wide ``OrderEventBus`` and
write audit rows into ``smart_money_follow_orders`` so the operator can
trace everything in one place.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
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
from .models import FollowListEntry, FollowOrder, Market, Trade

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Manual order placement
# ---------------------------------------------------------------------------

# Minimum size the operator must enter in the dashboard order form.
# Empty default for ``size_usdc`` is enforced at the API layer; we keep
# this constant here so the validation message stays consistent.
MIN_MANUAL_SIZE_USDC = 1.0
MAX_MANUAL_SIZE_USDC = 1000.0  # hard ceiling — anything bigger requires another path


def _publish(
    bus: OrderEventBus,
    *,
    order_id: str,
    leader_wallet: str,
    market_id: str | None,
    asset_id: str | None,
    side: str,
    status: OrderStatus,
    reason: str | None = None,
    data: dict[str, Any] | None = None,
) -> None:
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
        logger.exception("publish failed for order_id=%s", order_id)


def _resolve_asset_and_title(
    session: Session,
    *,
    condition_id: str,
    asset_id: str | None,
) -> tuple[str | None, str | None]:
    """Resolve the CLOB token id and human-readable market question.

    Order of preference:
      1. caller-provided ``asset_id`` (operator override)
      2. ``Market.token_yes`` / ``token_no`` if set (collector writes
         these from the CLOB token list)
      3. most recent ``Trade.token_id`` (collector-observed token)
    """
    market = session.execute(
        select(Market).where(Market.condition_id == condition_id)
    ).scalar_one_or_none()
    title = market.question if market is not None else None

    if asset_id:
        return asset_id, title

    if market is not None:
        # token_yes is index 0 of the outcomes array convention; default
        # to YES when the user did not specify a side preference.
        for tid in (market.token_yes, market.token_no):
            if tid:
                return tid, title

    sample = session.execute(
        select(Trade.token_id)
        .where(
            Trade.condition_id == condition_id,
            Trade.token_id.isnot(None),
        )
        .order_by(Trade.traded_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if sample:
        return sample, title
    return None, title


def submit_manual_order(
    session: Session,
    settings: SmartMoneySettings,
    *,
    condition_id: str,
    side: str,
    price: float,
    size_usdc: float,
    direction: str = "YES",
    asset_id: str | None = None,
    leader_wallet: str = "manual",
    bus: OrderEventBus | None = None,
) -> dict[str, Any]:
    """Place (or dry-run) a single mirror order from the dashboard.

    Args:
        session: SQLAlchemy session (caller owns the transaction).
        settings: SmartMoney settings (controls dry-run/live switch).
        condition_id, side, price, size_usdc: order details.
        direction: YES / NO (display only).
        asset_id: optional CLOB token id; if missing we look it up from
            the most recent ``Trade`` row for this condition_id.
        leader_wallet: the wallet *tag* to associate with the order
            (defaults to ``"manual"`` for tracking purposes).
        bus: optional OrderEventBus; defaults to the process bus.

    Returns:
        Dict with ``status`` (one of ``dry_run`` / ``submitted`` /
        ``error`` / ``skipped``), ``order_id``, free-form ``note``,
        and the resolved ``asset_id`` / ``size_shares``.
    """
    if bus is None:
        bus = get_order_event_bus()

    side_u = (side or "").upper()
    if side_u not in {"BUY", "SELL"}:
        return {"status": "skipped", "reason": "side must be BUY or SELL", "side": side}
    if not (0.0 < float(price) < 1.0):
        return {"status": "skipped", "reason": "price must be in (0, 1)", "price": price}
    sz = float(size_usdc)
    if sz < MIN_MANUAL_SIZE_USDC:
        return {
            "status": "skipped",
            "reason": f"size_usdc must be >= {MIN_MANUAL_SIZE_USDC}",
            "size_usdc": sz,
        }
    if sz > MAX_MANUAL_SIZE_USDC:
        return {
            "status": "skipped",
            "reason": f"size_usdc must be <= {MAX_MANUAL_SIZE_USDC}",
            "size_usdc": sz,
        }
    if not condition_id:
        return {"status": "skipped", "reason": "condition_id required"}

    resolved_asset, resolved_title = _resolve_asset_and_title(
        session, condition_id=condition_id, asset_id=(asset_id or "").strip() or None
    )

    order_id = new_order_id(leader_wallet or "manual", condition_id)
    size_shares = round(sz / float(price), 2) if float(price) > 0 else 0.0

    _publish(
        bus,
        order_id=order_id,
        leader_wallet=leader_wallet,
        market_id=condition_id,
        asset_id=resolved_asset,
        side=side_u,
        status=OrderStatus.PENDING,
        reason="manual dashboard order",
        data={
            "price": float(price),
            "size_usdc": sz,
            "size_shares": size_shares,
            "direction": direction,
            "title": resolved_title,
            # ``source="operator"`` is the marker the realtime-event
            # table uses to filter down to "things the operator did
            # themselves" (vs. auto-mirrored events the worker emits).
            "source": "operator",
            "entry_price": float(price),
        },
    )

    note = ""
    status = "dry_run"
    if settings.live_trade:
        _publish(
            bus,
            order_id=order_id,
            leader_wallet=leader_wallet,
            market_id=condition_id,
            asset_id=resolved_asset,
            side=side_u,
            status=OrderStatus.INFLIGHT,
            reason="submitting to CLOB",
            data={"source": "operator", "entry_price": float(price)},
        )
        ok, msg = _submit_live_clob(
            side=side_u,
            asset_id=resolved_asset,
            price=float(price),
            size_shares=size_shares,
            settings=settings,
        )
        status = "submitted" if ok else "error"
        note = msg
        _publish(
            bus,
            order_id=order_id,
            leader_wallet=leader_wallet,
            market_id=condition_id,
            asset_id=resolved_asset,
            side=side_u,
            status=OrderStatus.FILLED if ok else OrderStatus.FAILED,
            reason=msg if not ok else None,
            data={
                "clob_message": msg,
                "source": "operator",
                "entry_price": float(price),
            },
        )
    else:
        note = (
            f"MANUAL DRY-RUN side={side_u} price={float(price):.4f} "
            f"size=${sz:.2f} ({size_shares} shares) "
            f"asset_id={resolved_asset or '?'}"
        )
        logger.info("manual order DRY-RUN %s", note)
        _publish(
            bus,
            order_id=order_id,
            leader_wallet=leader_wallet,
            market_id=condition_id,
            asset_id=resolved_asset,
            side=side_u,
            status=OrderStatus.MIRRORED,
            reason="dry-run simulated",
            data={"note": note, "source": "operator", "entry_price": float(price)},
        )

    session.add(
        FollowOrder(
            signal_id=0,  # 0 = manual / not signal-driven
            wallet=leader_wallet,
            condition_id=condition_id,
            direction=direction,
            token_id=resolved_asset,
            side=side_u,
            price=float(price),
            size_usdc=sz,
            status=status,
            note=note,
        )
    )
    return {
        "status": status,
        "order_id": order_id,
        "note": note,
        "asset_id": resolved_asset,
        "size_shares": size_shares,
    }


def _submit_live_clob(
    *,
    side: str,
    asset_id: str | None,
    price: float,
    size_shares: float,
    settings: SmartMoneySettings,
) -> tuple[bool, str]:
    """Submit a real order to the CLOB.

    Mirrors ``executor._submit_live`` — uses the official ``polymarket``
    SDK's ``SecureClient`` (>= 0.2.0) so the deposit-wallet / POLY_1271
    flow (sig_type=3) works correctly.  The legacy ``py_clob_client``
    SDK doesn't support that signature type and every order is rejected
    with "A private key is needed…".

    Returns (success, message).
    """
    if not asset_id:
        return False, "missing asset_id, cannot place live order"
    if not settings.polymarket_private_key:
        return False, (
            "polymarket credentials missing: set "
            "SMART_MONEY_POLYMARKET_PRIVATE_KEY"
        )
    try:
        from polymarket import SecureClient  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return False, f"polymarket-client not installed: {exc!r}"

    try:
        client = SecureClient.create(private_key=settings.polymarket_private_key)
        wallet = client.wallet
        logger.info(
            "manual live init: wallet=%s wallet_type=%s",
            wallet, client.wallet_type,
        )
        resp = client.place_limit_order(
            token_id=asset_id,
            side=side.upper(),
            price=str(price),
            size=str(size_shares),
        )
        if getattr(resp, "ok", False):
            order_id = getattr(resp, "order_id", None) or "<unknown>"
            msg = f"order_id={order_id} status={getattr(resp, 'status', '?')}"
            return True, msg
        reason = getattr(resp, "message", None) or getattr(resp, "errorMsg", None)
        code = getattr(resp, "code", None)
        return False, f"rejected by CLOB: code={code} message={reason}"
    except Exception as exc:  # noqa: BLE001
        return False, f"clob submit failed: {exc!r}"


# ---------------------------------------------------------------------------
# Leader-sales detector
# ---------------------------------------------------------------------------


def detect_leader_sales(
    session: Session,
    settings: SmartMoneySettings,
    *,
    lookback_minutes: int = 30,
    bus: OrderEventBus | None = None,
) -> dict[str, Any]:
    """Scan recent ``Trade`` rows for follow-list wallets that SELL'd.

    "Recent" = ``traded_at >= now - lookback_minutes``.

    A SELL counts only when:
      * the wallet is on the current follow list (already vetted), AND
      * there is a matching open ``FollowOrder`` from our bot on the
        same ``condition_id`` (we are holding a mirrored long).

    Result: we publish one ``OrderStatus.PARTIAL`` event per detected
    SELL (treated as "leader has exited; consider closing") and send a
    Feishu alert if configured.

    Returns a dict with the list of detected sales for the dashboard
    panel plus a few counters.
    """
    if bus is None:
        bus = get_order_event_bus()

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)

    follow_rows = session.execute(
        select(FollowListEntry.wallet, FollowListEntry.username)
    ).all()
    follow_wallets = {w for (w, _) in follow_rows if w}
    if not follow_wallets:
        return {"sales": [], "skipped": True, "reason": "follow_list empty"}

    # Markets where the bot has an open mirrored BUY FollowOrder.
    open_orders = session.execute(
        select(
            FollowOrder.condition_id,
            FollowOrder.wallet,
            FollowOrder.token_id,
            FollowOrder.price,
            FollowOrder.size_usdc,
            FollowOrder.id,
            FollowOrder.direction,
        ).where(
            FollowOrder.side == "BUY",
            FollowOrder.status.in_(("dry_run", "submitted", "filled")),
            FollowOrder.signal_id != 0,
        )
    ).all()
    held: dict[str, list[dict[str, Any]]] = {}
    for cid, lw, tid, price, size, oid, direction in open_orders:
        held.setdefault(cid, []).append(
            {
                "order_id_db": oid,
                "leader_wallet": lw,
                "token_id": tid,
                "price": price,
                "size_usdc": size,
                "direction": direction,
            }
        )

    recent = (
        session.execute(
            select(Trade).where(
                Trade.wallet.in_(follow_wallets),
                Trade.side == "SELL",
                Trade.traded_at >= cutoff,
            )
        )
        .scalars()
        .all()
    )

    detected: list[dict[str, Any]] = []
    for trade in recent:
        matched = held.get(trade.condition_id)
        if not matched:
            continue
        for hold in matched:
            # Skip unless the leader who sold is the same one we mirror
            # on this market — avoids alerting on unrelated coincidental
            # sales from a different follow-list wallet.
            if hold["leader_wallet"] and hold["leader_wallet"] != trade.wallet:
                continue
            order_id = new_order_id(trade.wallet, trade.condition_id)
            summary = {
                "order_id": order_id,
                "leader_wallet": trade.wallet,
                "market_id": trade.condition_id,
                "asset_id": trade.token_id,
                "price": float(trade.price),
                "size": float(trade.size),
                "amount": float(trade.amount),
                "traded_at": trade.traded_at.isoformat() if trade.traded_at else None,
                "title": trade.title,
                "matched_held_order": hold["order_id_db"],
                "direction": hold["direction"],
            }
            detected.append(summary)
            _publish(
                bus,
                order_id=order_id,
                leader_wallet=trade.wallet,
                market_id=trade.condition_id,
                asset_id=trade.token_id,
                side="SELL",
                status=OrderStatus.PARTIAL,
                reason="leader closed position while we are still in",
                data={
                    "price": float(trade.price),
                    "size": float(trade.size),
                    "amount": float(trade.amount),
                    "traded_at": trade.traded_at.isoformat() if trade.traded_at else None,
                    "title": trade.title,
                    "mirror_direction": hold["direction"],
                },
            )
            if settings.feishu_webhook_url:
                _send_leader_sale_alert(
                    settings.feishu_webhook_url,
                    leader_wallet=trade.wallet,
                    market_title=trade.title or trade.condition_id,
                    condition_id=trade.condition_id,
                    price=float(trade.price),
                    size=float(trade.size),
                    amount=float(trade.amount),
                    held_size=hold["size_usdc"],
                )

    return {
        "sales": detected,
        "count": len(detected),
        "follow_wallets": len(follow_wallets),
        "held_markets": len(held),
        "lookback_minutes": lookback_minutes,
    }


def _send_leader_sale_alert(
    webhook_url: str,
    *,
    leader_wallet: str,
    market_title: str,
    condition_id: str,
    price: float,
    size: float,
    amount: float,
    held_size: float,
) -> bool:
    """Best-effort Feishu card for "leader sold".  Silent on transport errors."""
    title = "Leader exited — consider closing mirror"
    body = (
        f"**Leader wallet**: `{leader_wallet[:10]}`…\n"
        f"**Market**: {market_title}\n"
        f"**condition_id**: `{condition_id}`\n\n"
        f"**Leader action**: SELL @ {price:.4f} × {size:.2f} shares (~ ${amount:.2f})\n"
        f"**Your mirror**: still BUY ${held_size:.2f} on this market\n\n"
        f"Consider closing immediately or review manually."
    )
    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "red",
            },
            "elements": [
                {"tag": "markdown", "content": body},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "Open dashboard"},
                            "type": "default",
                            "url": "http://localhost:8088/?tab=follow",
                        }
                    ],
                },
            ],
        },
    }
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            return resp.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, Exception):  # noqa: BLE001
        logger.exception("failed to send leader-sale feishu alert")
        return False


__all__ = [
    "MAX_MANUAL_SIZE_USDC",
    "MIN_MANUAL_SIZE_USDC",
    "detect_leader_sales",
    "submit_manual_order",
]
