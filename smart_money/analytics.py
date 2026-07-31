from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, select, text
from sqlalchemy.orm import Session

from .config import SmartMoneySettings
from .models import (
    ClosedPosition,
    CurrentPosition,
    Market,
    Trade,
    Trader,
)

logger = logging.getLogger(__name__)


def _percent(count: int, total: int) -> float:
    return round(count / total * 100, 2) if total else 0.0


def _fmt_wallet(wallet: str) -> str:
    if len(wallet) <= 10:
        return wallet
    return f"{wallet[:6]}...{wallet[-4:]}"


class SmartMoneyAnalytics:
    """Answer the six Phase 1 questions using data already stored in PostgreSQL."""

    def __init__(self, session: Session, settings: SmartMoneySettings) -> None:
        self.session = session
        self.s = settings

    # ------------------------------------------------------------------
    # Q1: Who are the top-50 most profitable accounts in the last 90 days?
    # ------------------------------------------------------------------

    def top_profitable_traders(self, *, top_n: int = 50) -> list[dict[str, Any]]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.s.activity_lookback_days)
        # Realized PnL from closed positions within the lookback window
        pnl_cte = (
            select(
                ClosedPosition.wallet,
                func.sum(ClosedPosition.realized_pnl).label("realized_pnl"),
                func.sum(func.abs(ClosedPosition.total_bought)).label("total_spent"),
                func.count(ClosedPosition.fingerprint).label("closed_count"),
            )
            .where(ClosedPosition.closed_at >= cutoff)
            .group_by(ClosedPosition.wallet)
            .cte("pnl_cte")
        )
        # Also include open positions' unrealized cash PnL
        pos_cte = (
            select(
                CurrentPosition.wallet,
                func.sum(CurrentPosition.cash_pnl).label("open_pnl"),
            )
            .group_by(CurrentPosition.wallet)
            .cte("pos_cte")
        )
        stmt = (
            select(
                Trader.wallet,
                Trader.username,
                Trader.pseudonym,
                Trader.profile_image,
                Trader.verified,
                func.coalesce(pnl_cte.c.realized_pnl, 0).label("realized_pnl"),
                func.coalesce(pnl_cte.c.total_spent, 0).label("total_spent"),
                func.coalesce(pos_cte.c.open_pnl, 0).label("open_pnl"),
                func.coalesce(pnl_cte.c.closed_count, 0).label("closed_count"),
            )
            .join(pnl_cte, pnl_cte.c.wallet == Trader.wallet, isouter=True)
            .join(pos_cte, pos_cte.c.wallet == Trader.wallet, isouter=True)
            .where(Trader.tracked.is_(True))
            .order_by((func.coalesce(pnl_cte.c.realized_pnl, 0) + func.coalesce(pos_cte.c.open_pnl, 0)).desc())
            .limit(top_n)
        )
        rows = self.session.execute(stmt).all()
        return [
            {
                "rank": idx + 1,
                "wallet": _fmt_wallet(row.wallet),
                "wallet_full": row.wallet,
                "username": row.username or row.pseudonym or "—",
                "verified": row.verified,
                "profile_image": row.profile_image,
                "realized_pnl": float(row.realized_pnl or 0),
                "open_pnl": float(row.open_pnl or 0),
                "total_pnl": float((row.realized_pnl or 0) + (row.open_pnl or 0)),
                "total_spent": float(row.total_spent or 0),
                "roi": (
                    round(
                        float(row.realized_pnl or 0) / float(row.total_spent or 1) * 100,
                        2,
                    )
                    if (row.total_spent or 0) > 0
                    else 0.0
                ),
                "closed_count": row.closed_count or 0,
            }
            for idx, row in enumerate(rows)
        ]

    def top_window_winners(
        self, *, window_days: int = 30, top_n: int = 50
    ) -> list[dict[str, Any]]:
        """Top-N traders by **window** total PnL (last 30 days by default).

        This is the user-facing list of "recent winners" — distinct from
        :meth:`top_profitable_traders` which uses an all-time cumulative
        view.  Data is sourced from ``smart_money_window_scores`` which
        is recomputed on every scoring tick by ``window_scoring.py``.

        Falls back to an in-memory computation if the table is empty
        for the requested window.
        """
        from .models import WindowScore
        from .window_scoring import _compute_window_scores

        rows = self.session.execute(
            select(WindowScore)
            .where(WindowScore.window_days == window_days)
            .order_by(WindowScore.total_pnl.desc())
            .limit(top_n)
        ).scalars().all()
        if rows:
            return [
                {
                    "rank": i + 1,
                    "wallet": _fmt_wallet(r.wallet),
                    "wallet_full": r.wallet,
                    "username": r.username or r.pseudonym or "—",
                    "verified": r.verified,
                    "realized_pnl": float(r.realized_pnl or 0),
                    "unrealized_pnl": float(r.unrealized_pnl or 0),
                    "total_pnl": float(r.total_pnl or 0),
                    "roi_pct": float(r.roi_pct or 0),
                    "win_rate": float(r.win_rate or 0),
                    "trade_count": int(r.trade_count or 0),
                    "closed_count": int(r.closed_count or 0),
                    "open_position_count": int(r.open_position_count or 0),
                    "days_active": int(r.days_active or 0),
                    "days_since_active": float(r.days_since_active or 999),
                    "smart_window_score": float(r.smart_window_score or 0),
                    "top_category": r.top_category,
                    "window_days": r.window_days,
                }
                for i, r in enumerate(rows)
            ]
        # Fallback: recompute on the fly (and don't persist).
        profiles = _compute_window_scores(
            self.session, window_days=window_days
        )[:top_n]
        return [
            {
                "rank": p["rank"],
                "wallet": _fmt_wallet(p["wallet"]),
                "wallet_full": p["wallet"],
                "username": p["username"] or p["pseudonym"] or "—",
                "verified": bool(p["verified"]),
                "realized_pnl": float(p["realized_pnl"]),
                "unrealized_pnl": float(p["unrealized_pnl"]),
                "total_pnl": float(p["total_pnl"]),
                "roi_pct": float(p["roi_pct"]),
                "win_rate": float(p["win_rate"]),
                "trade_count": int(p["trade_count"]),
                "closed_count": int(p["closed_count"]),
                "open_position_count": int(p["open_position_count"]),
                "days_active": int(p["days_active"]),
                "days_since_active": float(p["days_since_active"]),
                "smart_window_score": float(p["smart_window_score"]),
                "top_category": p["top_category"],
                "window_days": p["window_days"],
            }
            for p in profiles
        ]

    # ------------------------------------------------------------------
    # Q2: What markets do they primarily trade?
    # ------------------------------------------------------------------

    def market_preferences(
        self,
        wallets: list[str] | None = None,
        *,
        top_n: int = 20,
    ) -> dict[str, Any]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.s.activity_lookback_days)
        q = (
            select(
                Market.condition_id,
                Market.question,
                Market.category,
                Market.slug,
                func.count(Trade.fingerprint).label("trade_count"),
                func.sum(func.abs(Trade.amount)).label("total_volume"),
                func.count(func.distinct(Trade.wallet)).label("unique_traders"),
            )
            .join(Trade, Trade.condition_id == Market.condition_id)
            .join(Trader, Trader.wallet == Trade.wallet)
            .where(
                Trader.tracked.is_(True),
                Trade.traded_at >= cutoff,
            )
        )
        if wallets:
            q = q.where(Trade.wallet.in_(wallets))
        q = q.group_by(Market.condition_id, Market.question, Market.category, Market.slug)
        q = q.order_by(func.sum(func.abs(Trade.amount)).desc()).limit(top_n)
        rows = self.session.execute(q).all()
        return {
            "items": [
                {
                    "rank": idx + 1,
                    "condition_id": row.condition_id,
                    "question": (row.question or "")[:120],
                    "category": row.category or "—",
                    "slug": row.slug,
                    "trade_count": row.trade_count or 0,
                    "total_volume": float(row.total_volume or 0),
                    "unique_traders": row.unique_traders or 0,
                }
                for idx, row in enumerate(rows)
            ],
            "category_summary": self._category_summary(wallets, cutoff),
        }

    def _category_summary(
        self,
        wallets: list[str] | None,
        cutoff: datetime,
    ) -> list[dict]:
        q = (
            select(
                Market.category,
                func.count(func.distinct(Trade.wallet)).label("trader_count"),
                func.sum(func.abs(Trade.amount)).label("volume"),
            )
            .join(Trade, Trade.condition_id == Market.condition_id)
            .join(Trader, Trader.wallet == Trade.wallet)
            .where(Trader.tracked.is_(True), Trade.traded_at >= cutoff)
        )
        if wallets:
            q = q.where(Trade.wallet.in_(wallets))
        q = q.group_by(Market.category).order_by(func.sum(func.abs(Trade.amount)).desc())
        rows = self.session.execute(q).all()
        return [
            {
                "category": row.category or "—",
                "trader_count": row.trader_count or 0,
                "volume": float(row.volume or 0),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Q3: Average lead time before placing bets (avg time before market end)
    # ------------------------------------------------------------------

    def avg_lead_time(self, wallets: list[str] | None = None) -> dict[str, Any]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.s.activity_lookback_days)
        q = (
            select(
                Trade.traded_at.label("traded_at"),
                Market.end_time.label("end_time"),
            )
            .select_from(Trade)
            .join(Market, Market.condition_id == Trade.condition_id, isouter=True)
            .join(Trader, Trader.wallet == Trade.wallet)
            .where(
                Trader.tracked.is_(True),
                Trade.traded_at >= cutoff,
                Market.end_time.isnot(None),
            )
        )
        if wallets:
            q = q.where(Trade.wallet.in_(wallets))
        leads_hours: list[float] = []
        for row in self.session.execute(q).all():
            traded_at = row.traded_at
            end_time = row.end_time
            if traded_at is None or end_time is None:
                continue
            delta = end_time - traded_at
            hours = delta.total_seconds() / 3600.0
            if hours >= 0:
                leads_hours.append(hours)
        if not leads_hours:
            return {"avg_lead_hours": 0.0, "min_lead_hours": 0.0, "max_lead_hours": 0.0, "total_trades": 0}
        return {
            "avg_lead_hours": round(sum(leads_hours) / len(leads_hours), 2),
            "min_lead_hours": round(min(leads_hours), 2),
            "max_lead_hours": round(max(leads_hours), 2),
            "total_trades": len(leads_hours),
        }

    # ------------------------------------------------------------------
    # Q4: Price distribution of their bets
    # ------------------------------------------------------------------

    def price_distribution(
        self,
        wallets: list[str] | None = None,
        *,
        bin_size: float = 0.05,
    ) -> dict[str, Any]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.s.activity_lookback_days)
        q = (
            select(
                Trade.side,
                func.count(Trade.fingerprint).label("count"),
                func.sum(func.abs(Trade.amount)).label("volume"),
                func.avg(Trade.price).label("avg_price"),
            )
            .join(Trader, Trader.wallet == Trade.wallet)
            .where(Trader.tracked.is_(True), Trade.traded_at >= cutoff)
        )
        if wallets:
            q = q.where(Trade.wallet.in_(wallets))
        q = q.group_by(Trade.side)
        sides = {row.side: row for row in self.session.execute(q).all()}

        # Per-bin breakdown
        bins = []
        lo = 0.0
        while lo < 1.0:
            hi = round(lo + bin_size, 2)
            for side, label in [("BUY", "YES"), ("SELL", "NO")]:
                qb = (
                    select(
                        func.count(Trade.fingerprint).label("count"),
                        func.sum(func.abs(Trade.amount)).label("volume"),
                    )
                    .select_from(Trade)
                    .join(Trader, Trader.wallet == Trade.wallet)
                    .where(
                        Trader.tracked.is_(True),
                        Trade.traded_at >= cutoff,
                        Trade.side == side,
                        Trade.price >= lo,
                        Trade.price < hi,
                    )
                )
                if wallets:
                    qb = qb.where(Trade.wallet.in_(wallets))
                row = self.session.execute(qb).first()
                bins.append({
                    "bin": f"{lo:.2f}-{hi:.2f}",
                    "side": label,
                    "count": row.count or 0,
                    "volume": float(row.volume or 0),
                })
            lo = hi

        total_buy_count = sides.get("BUY", None)
        total_sell_count = sides.get("SELL", None)
        return {
            "bins": bins,
            "summary": {
                "YES": {
                    "total_count": total_buy_count.count if total_buy_count else 0,
                    "total_volume": float(total_buy_count.volume if total_buy_count else 0),
                    "avg_price": float(total_buy_count.avg_price if total_buy_count else 0),
                },
                "NO": {
                    "total_count": total_sell_count.count if total_sell_count else 0,
                    "total_volume": float(total_sell_count.volume if total_sell_count else 0),
                    "avg_price": float(total_sell_count.avg_price if total_sell_count else 0),
                },
            },
        }

    # ------------------------------------------------------------------
    # Q5: Which top traders are currently placing bets?
    # ------------------------------------------------------------------

    def current_bets(self, hours: int | None = None) -> list[dict[str, Any]]:
        cutoff = datetime.now(timezone.utc) - timedelta(
            hours=hours or self.s.recent_trade_hours
        )
        stmt = (
            select(
                CurrentPosition.wallet,
                CurrentPosition.condition_id,
                CurrentPosition.outcome,
                CurrentPosition.outcome_index,
                CurrentPosition.size,
                CurrentPosition.avg_price,
                CurrentPosition.current_price,
                CurrentPosition.cash_pnl,
                CurrentPosition.title,
                CurrentPosition.slug,
                CurrentPosition.event_slug,
                CurrentPosition.end_time,
                CurrentPosition.observed_at,
                Trader.username,
                Trader.pseudonym,
                Trader.verified,
                func.max(Trade.traded_at).label("last_trade_at"),
            )
            .join(Trader, Trader.wallet == CurrentPosition.wallet)
            .outerjoin(
                Trade,
                (Trade.wallet == CurrentPosition.wallet)
                & (Trade.condition_id == CurrentPosition.condition_id)
                & (Trade.traded_at >= cutoff),
            )
            .where(
                Trader.tracked.is_(True),
                CurrentPosition.size > 0,
            )
            .group_by(
                CurrentPosition.wallet,
                CurrentPosition.condition_id,
                CurrentPosition.outcome,
                CurrentPosition.outcome_index,
                CurrentPosition.size,
                CurrentPosition.avg_price,
                CurrentPosition.current_price,
                CurrentPosition.cash_pnl,
                CurrentPosition.title,
                CurrentPosition.slug,
                CurrentPosition.event_slug,
                CurrentPosition.end_time,
                CurrentPosition.observed_at,
                Trader.username,
                Trader.pseudonym,
                Trader.verified,
            )
            .order_by(func.abs(CurrentPosition.size * CurrentPosition.current_price).desc())
            .limit(100)
        )
        rows = self.session.execute(stmt).all()
        return [
            {
                "wallet": _fmt_wallet(row.wallet),
                "wallet_full": row.wallet,
                "username": row.username or row.pseudonym or "—",
                "verified": row.verified,
                "market": (row.title or row.slug or row.condition_id)[:100],
                "condition_id": row.condition_id,
                "slug": row.event_slug or row.slug,
                "market_slug": row.slug,
                "event_slug": row.event_slug,
                "outcome": row.outcome,
                "direction": "YES" if row.outcome_index in (None, 0) else "NO",
                "size": float(row.size or 0),
                "avg_price": float(row.avg_price or 0),
                "current_price": float(row.current_price or 0),
                "position_value": float((row.size or 0) * (row.current_price or 0)),
                "unrealized_pnl": float(row.cash_pnl or 0),
                "end_time": row.end_time.isoformat() if row.end_time else None,
                "last_trade_at": row.last_trade_at.isoformat() if row.last_trade_at else None,
                "still_active": (
                    row.last_trade_at >= cutoff if row.last_trade_at else False
                ),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Q6: Do multiple top traders agree on a direction?
    # ------------------------------------------------------------------

    def consensus_signals(self) -> list[dict[str, Any]]:
        cutoff = datetime.now(timezone.utc) - timedelta(
            hours=self.s.recent_trade_hours
        )
        stmt = (
            select(
                CurrentPosition.condition_id,
                CurrentPosition.outcome,
                CurrentPosition.event_slug,
                CurrentPosition.slug,
                func.count(func.distinct(CurrentPosition.wallet)).label("trader_count"),
                func.sum(func.abs(CurrentPosition.size * CurrentPosition.current_price)).label(
                    "total_value"
                ),
                func.sum(CurrentPosition.cash_pnl).label("total_unrealized_pnl"),
                func.avg(CurrentPosition.avg_price).label("avg_entry_price"),
                CurrentPosition.title,
                CurrentPosition.end_time,
                func.count(func.distinct(
                    case(
                        (Trade.traded_at >= cutoff, Trade.wallet),
                        else_=None,
                    )
                )).label("recent_traders"),
            )
            .join(Trader, Trader.wallet == CurrentPosition.wallet)
            .outerjoin(
                Trade,
                (Trade.wallet == CurrentPosition.wallet)
                & (Trade.condition_id == CurrentPosition.condition_id)
                & (Trade.traded_at >= cutoff),
            )
            .where(
                Trader.tracked.is_(True),
                CurrentPosition.size > 0,
            )
            .group_by(
                CurrentPosition.condition_id,
                CurrentPosition.outcome,
                CurrentPosition.event_slug,
                CurrentPosition.slug,
                CurrentPosition.title,
                CurrentPosition.end_time,
            )
            .having(func.count(func.distinct(CurrentPosition.wallet)) >= self.s.min_consensus_traders)
            .order_by(
                func.sum(func.abs(CurrentPosition.size * CurrentPosition.current_price)).desc()
            )
            .limit(30)
        )
        rows = self.session.execute(stmt).all()
        signals = []
        for row in rows:
            direction = "YES" if (row.outcome or "").upper() in ("YES", "BUY", "1", "0") else "NO"
            signals.append({
                "condition_id": row.condition_id,
                "slug": row.event_slug or row.slug,  # event slug is the right URL for /event/...
                "market_slug": row.slug,
                "event_slug": row.event_slug,
                "market": (row.title or row.slug or row.condition_id)[:100],
                "direction": direction,
                "outcome": row.outcome,
                "trader_count": row.trader_count or 0,
                "total_value": float(row.total_value or 0),
                "total_unrealized_pnl": float(row.total_unrealized_pnl or 0),
                "avg_entry_price": float(row.avg_entry_price or 0),
                "recent_traders": row.recent_traders or 0,
                "end_time": row.end_time.isoformat() if row.end_time else None,
                "confidence": min(
                    0.99,
                    round(
                        float(row.trader_count or 1)
                        / max(1, float(row.trader_count or 1) + 1)
                        + 0.5
                        * min(1.0, float(row.total_value or 0) / 50000),
                        2,
                    ),
                ),
            })
        return signals

    # ------------------------------------------------------------------
    # Dashboard snapshot
    # ------------------------------------------------------------------

    def dashboard_snapshot(self) -> dict[str, Any]:
        wallets = [
            r["wallet_full"]
            for r in self.top_profitable_traders(top_n=self.s.tracked_wallet_limit)
        ]
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.s.activity_lookback_days)
        recent_cutoff = datetime.now(timezone.utc) - timedelta(hours=self.s.recent_trade_hours)
        total_traders = self.session.execute(
            select(func.count(Trader.wallet)).where(Trader.tracked.is_(True))
        ).scalar() or 0
        active_now = self.session.execute(
            select(func.count(func.distinct(Trade.wallet))).where(Trade.traded_at >= recent_cutoff)
        ).scalar() or 0
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "lookback_days": self.s.activity_lookback_days,
            "recent_hours": self.s.recent_trade_hours,
            "total_tracked": total_traders,
            "active_recent": active_now,
            "top_50": self.top_profitable_traders(top_n=50),
            "top_window_30d": self.top_window_winners(window_days=30, top_n=50),
            "market_preferences": self.market_preferences(wallets=wallets[:200] or None, top_n=20),
            "lead_time": self.avg_lead_time(wallets=wallets[:200] or None),
            "current_bets": self.current_bets(),
            "consensus": self.consensus_signals(),
        }
