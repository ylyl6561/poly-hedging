"""Compute *short-window* trader profiles from trades / closed /
open positions.

This complements :mod:`smart_money.scoring` (which is lifetime).
The :class:`~smart_money.models.WindowScore` table is what the
dashboard's **"Trader Scores · 智能资金画像"** panel and the
**follow-list filter** now key off.

User requirement (Phase 5)
-------------------------
*"重点看收益曲线 — 持续盈利周期 ≥ 30 天；Top50 钱包看盈利稳定性 与
收益连续性；30 天交易 < 30 笔不要；偏好稳定、低回撤的隐形高手。"*

So the filter prefers wallets with:

1. **Profitability persistence** — *every* day inside the 30-day
   window must net positive realised PnL (``loss_days == 0``).  A
   single losing day disqualifies the wallet.
2. **Stability** — ``daily_pnl_stddev`` should be low relative to
   ``avg_daily_pnl``.  We capture the ratio indirectly in the
   composite score; high stddev = "lucky single spike" wallets.
3. **Drawdown control** — ``max_drawdown_pct`` (peak-to-trough of the
   cumulative PnL curve) should be small.  Defaults cap at 20% in the
   follow filter.
4. **Trade frequency** — between 3 and 30 trades in the window.
   <3 = not enough data; >30 = high-frequency day-trader / 已不算"稳定".
5. **Recency** — last trade within ``follow_max_idle_days``.
6. **Activity span** — the *earliest* trade inside the window must be
   at least ``follow_min_period_days`` ago (default 14d).  This kills
   "single-week bursts" where a wallet shows up, prints 200% PnL in
   5 days, and disappears.

Composite scoring formula (matches user intent)
-----------------------------------------------
::

    window_score = round(
        100 * (
          + 0.30 * roi_norm           # window ROI
          + 0.20 * pnl_norm           # absolute PnL (winsorized by log)
          + 0.15 * profit_days_norm   # ratio of profit days
          + 0.15 * streak_norm        # longest win streak / 30
          + 0.10 * drawdown_norm      # (1 - max_dd_pct/100), clamped
          + 0.10 * recency_norm       # half-life 7 days
        ),
        2,
    )

Where each ``*_norm`` is compressed into [0, 1] via a sigmoid or
linear ramp.  Higher score = more "stable, persistent winner".
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .models import (
    ClosedPosition,
    CurrentPosition,
    Trader,
    Trade,
    WindowScore,
)

logger = logging.getLogger(__name__)


_DEFAULT_WINDOW_DAYS = 30


def _sigmoid(x: float, *, k: float = 1.0, mid: float = 0.0) -> float:
    return 1.0 / (1.0 + math.exp(-k * (x - mid)))


def _norm_roi(roi_pct: float) -> float:
    """ROI in percent → [0, 1]."""
    return _sigmoid(roi_pct / 100.0, k=2.0, mid=0.5)


def _norm_pnl(total_pnl: float) -> float:
    """Absolute PnL (USDC) → [0, 1]."""
    if total_pnl <= 0:
        return 0.0
    return min(1.0, math.log10(total_pnl / 1000.0 + 1.0) / 6.0)


def _norm_recency(days_since_active: float, *, half_life_days: float = 7.0) -> float:
    """Days since last trade → [0, 1] (half-life 7 days)."""
    if days_since_active is None or days_since_active < 0:
        return 0.0
    return 0.5 ** (days_since_active / half_life_days)


def _norm_profit_days_ratio(profit_days: int, days_active: int) -> float:
    """Profit-days / active-days → [0, 1].

    We require *every* active day to be a profit day for full credit;
    this is the "no losing days" rule.
    """
    if days_active <= 0 or profit_days <= 0:
        return 0.0
    return min(1.0, profit_days / max(days_active, 1))


def _norm_streak(streak: int, *, max_days: int = 30) -> float:
    """Longest winning-day streak → [0, 1]."""
    if streak <= 0:
        return 0.0
    return min(1.0, streak / max_days)


def _norm_drawdown(max_dd_pct: float) -> float:
    """Max-drawdown percentage → [0, 1].  Lower drawdown → higher score."""
    if max_dd_pct is None or max_dd_pct < 0:
        return 0.0
    return max(0.0, min(1.0, 1.0 - max_dd_pct / 100.0))


def _max_drawdown_pct(cumulative: list[float]) -> float:
    """Given a chronological list of cumulative-PnL values, return
    ``max_drawdown_pct = max((peak - trough) / peak * 100)``.

    Returns 0.0 for an empty or monotonically-rising series.
    """
    if not cumulative:
        return 0.0
    peak = cumulative[0]
    max_dd = 0.0
    for v in cumulative:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak * 100.0
            if dd > max_dd:
                max_dd = dd
    return max_dd


def _longest_win_streak(daily_pnl: list[float]) -> int:
    """Longest run of consecutive strictly-positive days."""
    best = 0
    cur = 0
    for v in daily_pnl:
        if v > 0:
            cur += 1
            if cur > best:
                best = cur
        else:
            cur = 0
    return best


def _compute_window_scores(
    session: Session,
    *,
    window_days: int = _DEFAULT_WINDOW_DAYS,
) -> list[dict[str, Any]]:
    """Recompute ``smart_money_window_scores`` for every tracked
    trader from trades / closed positions within ``window_days``.
    """
    if window_days <= 0:
        raise ValueError(f"window_days must be > 0, got {window_days}")

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)

    # --- (1) Closed positions → realized_pnl / closed_count / per-day PnL ---
    closed_q = (
        select(
            ClosedPosition.wallet.label("wallet"),
            ClosedPosition.closed_at.label("closed_at"),
            ClosedPosition.realized_pnl.label("realized_pnl"),
            ClosedPosition.fingerprint.label("fp"),
        )
        .where(ClosedPosition.closed_at >= cutoff)
    )
    closed_rows: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"realized_pnl": 0.0, "closed_count": 0, "win_count": 0}
    )
    daily_pnl: dict[str, dict[Any, float]] = defaultdict(lambda: defaultdict(float))
    for wallet, closed_at, realized_pnl, _fp in session.execute(closed_q).all():
        slot = closed_rows[wallet]
        slot["realized_pnl"] += float(realized_pnl or 0)
        slot["closed_count"] += 1
        if (realized_pnl or 0) > 0:
            slot["win_count"] += 1
        if closed_at is not None:
            if closed_at.tzinfo is None:
                closed_at = closed_at.replace(tzinfo=timezone.utc)
            daily_pnl[wallet][closed_at.date()] += float(realized_pnl or 0)

    # --- (2) Trades → trade_count / total_volume / dates / markets ---
    trade_q = (
        select(
            Trade.wallet.label("wallet"),
            Trade.fingerprint.label("fp"),
            Trade.amount.label("amount"),
            Trade.traded_at.label("traded_at"),
            Trade.condition_id.label("cid"),
        )
        .where(Trade.traded_at >= cutoff)
    )
    trade_rows: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "trade_count": 0, "total_volume": 0.0,
            "last_active_at": None, "earliest_trade_at": None,
            "trade_dates": set(), "markets": set(),
        }
    )
    for wallet, _fp, amount, traded_at, cid in session.execute(trade_q).all():
        slot = trade_rows[wallet]
        slot["trade_count"] += 1
        slot["total_volume"] += abs(float(amount or 0))
        if traded_at is not None:
            slot["trade_dates"].add(traded_at.date())
            last = slot["last_active_at"]
            if last is None or traded_at > last:
                slot["last_active_at"] = traded_at
            earliest = slot["earliest_trade_at"]
            if earliest is None or traded_at < earliest:
                slot["earliest_trade_at"] = traded_at
        if cid:
            slot["markets"].add(cid)

    # --- (3) Open positions (current) ---
    open_q = (
        select(
            CurrentPosition.wallet.label("wallet"),
            CurrentPosition.token_id,
            CurrentPosition.cash_pnl,
        )
        .where(CurrentPosition.size > 0)
    )
    open_rows: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"open_position_count": 0, "unrealized_pnl": 0.0}
    )
    for wallet, _tid, cash_pnl in session.execute(open_q).all():
        slot = open_rows[wallet]
        slot["open_position_count"] += 1
        slot["unrealized_pnl"] += float(cash_pnl or 0)

    # --- (4) Top category in window ---
    cat_q = text(
        """
        SELECT t.wallet, m.category, SUM(ABS(t.amount)) AS vol
        FROM smart_money_trades t
        JOIN smart_money_markets m ON m.condition_id = t.condition_id
        WHERE t.traded_at >= :cutoff
          AND m.category IS NOT NULL
        GROUP BY t.wallet, m.category
        """
    )
    cat_vol: dict[str, dict[str, float]] = {}
    for r in session.execute(cat_q, {"cutoff": cutoff}).all():
        cat_vol.setdefault(r.wallet, {})[r.category or "—"] = float(r.vol or 0)

    # --- (5) Trader identity (username etc.) ---
    wallet_set = (
        set(closed_rows) | set(trade_rows) | set(open_rows) | set(daily_pnl)
    )
    if not wallet_set:
        return []

    trader_rows_db = {
        r.wallet: r
        for r in session.execute(
            select(Trader).where(Trader.wallet.in_(wallet_set))
        ).scalars().all()
    }

    profiles: list[dict[str, Any]] = []
    for w in wallet_set:
        trade_row = trade_rows.get(w)
        closed_row = closed_rows.get(w)
        open_row = open_rows.get(w)
        trader = trader_rows_db.get(w)

        trade_count = int(trade_row["trade_count"]) if trade_row else 0
        total_volume = float(trade_row["total_volume"] or 0) if trade_row else 0.0
        last_active = trade_row["last_active_at"] if trade_row else None
        earliest_trade_at = (
            trade_row["earliest_trade_at"] if trade_row else None
        )
        days_active = (
            len(trade_row["trade_dates"]) if trade_row and trade_row["trade_dates"] else 0
        )
        markets_traded = (
            len(trade_row["markets"]) if trade_row and trade_row["markets"] else 0
        )

        if last_active is not None and last_active.tzinfo is None:
            last_active = last_active.replace(tzinfo=timezone.utc)
        if earliest_trade_at is not None and earliest_trade_at.tzinfo is None:
            earliest_trade_at = earliest_trade_at.replace(tzinfo=timezone.utc)
        days_since_active = (
            (now - last_active).total_seconds() / 86400.0
            if last_active is not None else 999.0
        )
        period_days = (
            (now - earliest_trade_at).total_seconds() / 86400.0
            if earliest_trade_at is not None else 0.0
        )

        realized_pnl = float(closed_row["realized_pnl"] or 0) if closed_row else 0.0
        closed_count = int(closed_row["closed_count"] or 0) if closed_row else 0
        win_count = int(closed_row["win_count"] or 0) if closed_row else 0
        win_rate = (win_count / closed_count) if closed_count else 0.0

        unrealized_pnl = float(open_row["unrealized_pnl"] or 0) if open_row else 0.0
        open_position_count = int(open_row["open_position_count"] or 0) if open_row else 0

        # ---- Stability / streak / drawdown metrics ----
        wallet_daily = sorted(daily_pnl.get(w, {}).items())
        per_day_pnl = [v for _, v in wallet_daily]
        profit_days = sum(1 for v in per_day_pnl if v > 0)
        loss_days = sum(1 for v in per_day_pnl if v < 0)
        streak = _longest_win_streak(per_day_pnl)
        cumulative: list[float] = []
        running = 0.0
        for v in per_day_pnl:
            running += v
            cumulative.append(running)
        max_dd_pct = _max_drawdown_pct(cumulative)
        if len(per_day_pnl) >= 2:
            mean = sum(per_day_pnl) / len(per_day_pnl)
            var = sum((v - mean) ** 2 for v in per_day_pnl) / len(per_day_pnl)
            daily_pnl_stddev = math.sqrt(var)
        else:
            daily_pnl_stddev = 0.0

        # ---- Totals ----
        total_pnl = realized_pnl + unrealized_pnl
        if total_volume > 0:
            roi_pct = max(-99.0, min(2000.0, realized_pnl / total_volume * 100.0))
        else:
            roi_pct = 0.0
        avg_daily_pnl = total_pnl / max(days_active, 1)

        cats = cat_vol.get(w, {})
        top_category = max(cats, key=cats.get) if cats else None

        composite = (
            0.30 * _norm_roi(roi_pct)
            + 0.20 * _norm_pnl(total_pnl)
            + 0.15 * _norm_profit_days_ratio(profit_days, days_active)
            + 0.15 * _norm_streak(streak)
            + 0.10 * _norm_drawdown(max_dd_pct)
            + 0.10 * _norm_recency(days_since_active)
        )
        score = round(composite * 100.0, 2)

        profiles.append({
            "wallet": w,
            "username": trader.username if trader else None,
            "pseudonym": trader.pseudonym if trader else None,
            "verified": bool(trader.verified) if trader else False,
            "window_days": window_days,
            "trade_count": trade_count,
            "closed_count": closed_count,
            "days_active": days_active,
            "markets_traded": markets_traded,
            "last_active_at": last_active,
            "earliest_trade_at": earliest_trade_at,
            "days_since_active": round(days_since_active, 2),
            "period_days": round(period_days, 2),
            "total_volume": round(total_volume, 2),
            "realized_pnl": round(realized_pnl, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "total_pnl": round(total_pnl, 2),
            "roi_pct": round(roi_pct, 2),
            "win_rate": round(win_rate, 4),
            "avg_daily_pnl": round(avg_daily_pnl, 2),
            "open_position_count": open_position_count,
            "profit_days": profit_days,
            "loss_days": loss_days,
            "longest_win_streak": streak,
            "max_drawdown_pct": round(max_dd_pct, 2),
            "daily_pnl_stddev": round(daily_pnl_stddev, 2),
            "top_category": top_category,
            "smart_window_score": score,
        })

    profiles.sort(key=lambda p: p["smart_window_score"], reverse=True)
    for i, p in enumerate(profiles, start=1):
        p["rank"] = i
    return profiles


def recompute_window_scores(
    session: Session,
    *,
    window_days: int = _DEFAULT_WINDOW_DAYS,
) -> dict[str, int]:
    """Wipe + repopulate ``smart_money_window_scores``.

    Returns ``{"scored": N, "eligible": N}`` so the caller can include
    in run history.
    """
    profiles = _compute_window_scores(session, window_days=window_days)
    if profiles:
        session.execute(WindowScore.__table__.delete())
        rows = [
            {k: v for k, v in p.items() if k in WindowScore.__table__.columns}
            for p in profiles
        ]
        session.bulk_save_objects([WindowScore(**r) for r in rows])
    eligible = sum(
        1
        for p in profiles
        if p["realized_pnl"] > 0
        and p["loss_days"] == 0
        and p["days_active"] >= 1
        and p["open_position_count"] >= 1
    )
    logger.info(
        "recompute_window_scores(window=%dd): scored=%d eligible=%d",
        window_days,
        len(profiles),
        eligible,
    )
    return {"scored": len(profiles), "eligible": eligible}