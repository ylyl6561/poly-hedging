"""Compute per-trader profiles from trades / closed / current positions.

This module answers the "build a trader image" requirement. It runs after the
``trades`` job so the data is fresh.

Profile components
------------------
* ``trade_count`` / ``closed_count`` / ``open_count`` — activity volume
* ``total_volume`` — sum of |amount| traded
* ``realized_pnl`` — sum of closed positions realized PnL
* ``unrealized_pnl`` — sum of current positions cash PnL
* ``win_rate`` — closed_count with realized_pnl > 0 / closed_count
* ``avg_win`` / ``avg_loss`` — average winning/losing trade
* ``avg_hold_hours`` — mean of (closed_at - first trade in that condition)
* ``max_drawdown`` — peak-to-trough cumulative PnL (running closed pnl)
* ``yes_ratio`` — BUY-share of trade count
* ``top_category`` — category with most volume for this wallet
* ``smart_money_score`` — composite in [0, 100]

Composite score formula
-----------------------
``0.40 * ROI_norm + 0.30 * win_rate + 0.20 * volume_norm + 0.10 * recency_norm``
where each ``*_norm`` is a sigmoid-style compression into [0, 1].
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, func, select, text
from sqlalchemy.orm import Session

from .models import (
    ClosedPosition,
    CurrentPosition,
    Market,
    Trader,
    TraderScore,
    Trade,
)

logger = logging.getLogger(__name__)


def _sigmoid(x: float, *, k: float = 1.0, mid: float = 0.0) -> float:
    """Smoothly compress ``x`` into [0, 1]."""
    import math

    return 1.0 / (1.0 + math.exp(-k * (x - mid)))


def _norm_roi(roi_pct: float) -> float:
    # 0% → 0.5, +100% → ~0.73, +500% → ~0.95
    return _sigmoid(roi_pct / 100.0, k=2.0, mid=0.5)


def _norm_volume(volume: float) -> float:
    # log scale so $10k → 0.5, $100k → 0.75, $1M → 0.9
    import math

    if volume <= 0:
        return 0.0
    return min(1.0, math.log10(volume / 1000.0 + 1.0) / 6.0)


def _norm_recency(last_active_at: datetime | None, *, half_life_days: float = 14.0) -> float:
    if not last_active_at:
        return 0.0
    now = datetime.now(timezone.utc)
    days = (now - last_active_at).total_seconds() / 86400.0
    if days < 0:
        days = 0.0
    return 0.5 ** (days / half_life_days)


def _max_drawdown(realized_series: list[float]) -> float:
    """Peak-to-trough on cumulative PnL — expects chronologically sorted deltas."""
    peak = 0.0
    cum = 0.0
    worst = 0.0
    for delta in realized_series:
        cum += delta
        if cum > peak:
            peak = cum
        drawdown = peak - cum
        if drawdown > worst:
            worst = drawdown
    return worst


def compute_trader_profiles(
    session: Session,
    *,
    lookback_days: int = 90,
) -> dict[str, int]:
    """Recompute ``smart_money_trader_scores`` for every tracked trader.

    Returns counters so the caller can include them in run history.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    recent_cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    # Realized PnL per wallet from closed positions
    realized_q = (
        select(
            ClosedPosition.wallet.label("wallet"),
            func.sum(ClosedPosition.realized_pnl).label("realized_pnl"),
            func.count(ClosedPosition.fingerprint).label("closed_count"),
            func.sum(case((ClosedPosition.realized_pnl > 0, 1), else_=0)).label("win_count"),
            func.avg(case((ClosedPosition.realized_pnl > 0, ClosedPosition.realized_pnl))).label(
                "avg_win"
            ),
            func.avg(case((ClosedPosition.realized_pnl <= 0, ClosedPosition.realized_pnl))).label(
                "avg_loss"
            ),
        )
        .where(ClosedPosition.closed_at >= cutoff)
        .group_by(ClosedPosition.wallet)
    )
    realized_rows = {r.wallet: r for r in session.execute(realized_q).all()}

    # Open unrealized per wallet
    open_q = (
        select(
            CurrentPosition.wallet.label("wallet"),
            func.sum(CurrentPosition.cash_pnl).label("unrealized_pnl"),
            func.count(CurrentPosition.wallet).label("open_count"),
        )
        .group_by(CurrentPosition.wallet)
    )
    open_rows = {r.wallet: r for r in session.execute(open_q).all()}

    # Trade activity per wallet
    trade_q = (
        select(
            Trade.wallet.label("wallet"),
            func.count(Trade.fingerprint).label("trade_count"),
            func.sum(func.abs(Trade.amount)).label("total_volume"),
            func.sum(case((Trade.side == "BUY", 1), else_=0)).label("buy_count"),
            func.max(Trade.traded_at).label("last_trade_at"),
            func.min(Trade.traded_at).label("first_trade_at"),
        )
        .where(Trade.traded_at >= cutoff)
        .group_by(Trade.wallet)
    )
    trade_rows = {r.wallet: r for r in session.execute(trade_q).all()}

    # Total spent per wallet — for ROI
    spent_q = (
        select(
            Trade.wallet.label("wallet"),
            func.sum(Trade.amount).label("net_spent"),
        )
        .where(Trade.traded_at >= cutoff, Trade.side == "BUY")
        .group_by(Trade.wallet)
    )
    spent_rows = {r.wallet: float(r.net_spent or 0) for r in session.execute(spent_q).all()}

    # Top category per wallet (most volume)
    cat_q = text(
        """
        SELECT t.wallet, m.category, SUM(ABS(t.amount)) AS vol
        FROM smart_money_trades t
        JOIN smart_money_markets m ON m.condition_id = t.condition_id
        WHERE t.traded_at >= :cutoff
        GROUP BY t.wallet, m.category
        """
    )
    cat_vol: dict[str, dict[str, float]] = {}
    for r in session.execute(cat_q, {"cutoff": cutoff}).all():
        cat_vol.setdefault(r.wallet, {})[r.category or "—"] = float(r.vol or 0)

    # Avg holding time: for each closed position, take (closed_at - first_trade_at for same wallet+condition)
    # Then average across all closed positions
    hold_q = text(
        """
        SELECT cp.wallet,
               cp.condition_id,
               cp.closed_at,
               (SELECT MIN(t2.traded_at) FROM smart_money_trades t2
                 WHERE t2.wallet = cp.wallet AND t2.condition_id = cp.condition_id) AS first_trade_at
        FROM smart_money_closed_positions cp
        WHERE cp.closed_at >= :cutoff
        """
    )
    hold_seconds_by_wallet: dict[str, list[float]] = {}
    for r in session.execute(hold_q, {"cutoff": cutoff}).all():
        if not r.first_trade_at or not r.closed_at:
            continue
        delta = (r.closed_at - r.first_trade_at).total_seconds()
        if delta < 0:
            continue
        hold_seconds_by_wallet.setdefault(r.wallet, []).append(delta)

    # Drawdown: need per-wallet chronological realized_pnl series
    dd_q = text(
        """
        SELECT wallet, closed_at, realized_pnl
        FROM smart_money_closed_positions
        WHERE closed_at >= :cutoff
        ORDER BY wallet, closed_at
        """
    )
    dd_series: dict[str, list[float]] = {}
    for r in session.execute(dd_q, {"cutoff": cutoff}).all():
        if r.realized_pnl is None:
            continue
        dd_series.setdefault(r.wallet, []).append(float(r.realized_pnl))

    # Union of wallets seen in any source
    wallets: set[str] = set(realized_rows) | set(open_rows) | set(trade_rows)
    if not wallets:
        return {"scored": 0, "eligible": 0}

    # Map wallets to trader records
    trader_rows = {
        r.wallet: r
        for r in session.execute(
            select(Trader).where(Trader.wallet.in_(wallets))
        ).scalars().all()
    }

    profiles: list[dict[str, Any]] = []
    for w in wallets:
        trade_row = trade_rows.get(w)
        realized_row = realized_rows.get(w)
        open_row = open_rows.get(w)
        trader = trader_rows.get(w)

        trade_count = int(trade_row.trade_count) if trade_row else 0
        total_volume = float(trade_row.total_volume) if trade_row else 0.0
        buy_count = int(trade_row.buy_count) if trade_row else 0
        yes_ratio = (buy_count / trade_count) if trade_count else 0.5
        last_active = trade_row.last_trade_at if trade_row else None

        realized_pnl = float(realized_row.realized_pnl) if realized_row else 0.0
        closed_count = int(realized_row.closed_count) if realized_row else 0
        win_count = int(realized_row.win_count) if realized_row else 0
        avg_win = float(realized_row.avg_win) if realized_row and realized_row.avg_win else 0.0
        avg_loss = float(realized_row.avg_loss) if realized_row and realized_row.avg_loss else 0.0
        win_rate = (win_count / closed_count) if closed_count else 0.0

        unrealized_pnl = float(open_row.unrealized_pnl) if open_row else 0.0
        open_count = int(open_row.open_count) if open_row else 0

        net_spent = spent_rows.get(w, 0.0)
        # ROI = realized_pnl / spent (absolute), winsorize
        if net_spent > 0:
            roi_pct = max(-99.0, min(1000.0, realized_pnl / abs(net_spent) * 100.0))
        else:
            roi_pct = 0.0

        avg_hold = (
            sum(hold_seconds_by_wallet.get(w, [])) / len(hold_seconds_by_wallet.get(w, [])) / 3600.0
            if hold_seconds_by_wallet.get(w)
            else 0.0
        )
        drawdown = _max_drawdown(dd_series.get(w, []))

        cats = cat_vol.get(w, {})
        top_category = max(cats, key=cats.get) if cats else None

        # Eligibility: at least N closed positions to be considered "smart"
        eligible = closed_count >= 5 and total_volume >= 1000.0

        composite = (
            0.40 * _norm_roi(roi_pct)
            + 0.30 * win_rate
            + 0.20 * _norm_volume(total_volume)
            + 0.10 * _norm_recency(last_active)
        )
        score = round(composite * 100.0, 2)

        profiles.append(
            {
                "wallet": w,
                "username": trader.username if trader else None,
                "pseudonym": trader.pseudonym if trader else None,
                "verified": bool(trader.verified) if trader else False,
                "trade_count": trade_count,
                "closed_count": closed_count,
                "open_count": open_count,
                "total_volume": round(total_volume, 2),
                "realized_pnl": round(realized_pnl, 2),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "total_pnl": round(realized_pnl + unrealized_pnl, 2),
                "roi_pct": round(roi_pct, 2),
                "win_rate": round(win_rate, 4),
                "avg_win": round(avg_win, 2),
                "avg_loss": round(avg_loss, 2),
                "avg_hold_hours": round(avg_hold, 2),
                "max_drawdown": round(drawdown, 2),
                "yes_ratio": round(yes_ratio, 4),
                "top_category": top_category,
                "smart_money_score": score,
            }
        )

    profiles.sort(key=lambda p: p["smart_money_score"], reverse=True)
    for i, p in enumerate(profiles, start=1):
        p["rank"] = i

    # Upsert into trader_scores table
    if profiles:
        session.execute(TraderScore.__table__.delete())
        session.bulk_save_objects([TraderScore(**p) for p in profiles])

    eligible_count = sum(1 for p in profiles if p["closed_count"] >= 5 and p["total_volume"] >= 1000)
    logger.info(
        "compute_trader_profiles: scored=%d eligible=%d",
        len(profiles),
        eligible_count,
    )
    return {"scored": len(profiles), "eligible": eligible_count}
