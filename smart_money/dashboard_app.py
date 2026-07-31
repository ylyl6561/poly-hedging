from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from sqlalchemy import func, select, text as sql_text

from .analytics import SmartMoneyAnalytics
from .normalization import utc_now
from .collector import SmartMoneyCollector
from .config import SmartMoneySettings, get_settings
from .db import get_session_factory

# Ensure the project-root ``.env`` is loaded before the very first
# ``get_settings()`` call inside this module — LaunchAgent-managed
# workers don't inherit the shell's dotenv state.
from .cli import _bootstrap_dotenv  # noqa: E402

_bootstrap_dotenv()

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Polymarket Smart Money Tracker",
        version="0.1.0",
        description="Phase 1 read-only dashboard for top Polymarket traders.",
    )
    dashboard_dir: Path = settings.dashboard_dir
    dashboard_dir.mkdir(parents=True, exist_ok=True)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        try:
            with _session() as session:
                session.execute(sql_text("SELECT 1"))
            return {"status": "ok"}
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc))

    @app.get("/api/dashboard")
    def dashboard() -> dict[str, Any]:
        with _session() as session:
            return SmartMoneyAnalytics(session, settings).dashboard_snapshot()

    @app.get("/api/top-traders")
    def top_traders(top: int = 50) -> dict[str, Any]:
        if top < 1 or top > 500:
            raise HTTPException(status_code=400, detail="top must be 1..500")
        with _session() as session:
            items = SmartMoneyAnalytics(session, settings).top_profitable_traders(top_n=top)
        return {"items": items}

    @app.get("/api/top-traders/window")
    def top_traders_window(
        top: int = 50, window_days: int = 30
    ) -> dict[str, Any]:
        """Top-N traders sorted by realized PnL inside the
        ``window_days``-day window.  This is the user-facing
        "Q1 · 30 天盈利 Top 50" panel."""
        if top < 1 or top > 500:
            raise HTTPException(status_code=400, detail="top must be 1..500")
        if window_days not in (7, 14, 30, 60, 90):
            raise HTTPException(
                status_code=400, detail="window_days must be 7/14/30/60/90"
            )
        with _session() as session:
            items = SmartMoneyAnalytics(session, settings).top_window_winners(
                window_days=window_days, top_n=top
            )
        return {"items": items, "window_days": window_days}

    @app.get("/api/market-preferences")
    def market_preferences(wallets: str | None = None, top: int = 20) -> dict[str, Any]:
        if top < 1 or top > 200:
            raise HTTPException(status_code=400, detail="top must be 1..200")
        parsed = _parse_wallets(wallets)
        with _session() as session:
            return SmartMoneyAnalytics(session, settings).market_preferences(
                wallets=parsed, top_n=top
            )

    @app.get("/api/lead-time")
    def lead_time(wallets: str | None = None) -> dict[str, Any]:
        parsed = _parse_wallets(wallets)
        with _session() as session:
            return SmartMoneyAnalytics(session, settings).avg_lead_time(wallets=parsed)

    @app.get("/api/price-distribution")
    def price_distribution(wallets: str | None = None) -> dict[str, Any]:
        parsed = _parse_wallets(wallets)
        with _session() as session:
            return SmartMoneyAnalytics(session, settings).price_distribution(wallets=parsed)

    @app.get("/api/current-bets")
    def current_bets(hours: int | None = None) -> dict[str, Any]:
        if hours is not None and (hours < 1 or hours > 168):
            raise HTTPException(status_code=400, detail="hours must be 1..168")
        with _session() as session:
            items = SmartMoneyAnalytics(session, settings).current_bets(hours=hours)
        return {"items": items}

    @app.get("/api/consensus")
    def consensus() -> dict[str, Any]:
        with _session() as session:
            items = SmartMoneyAnalytics(session, settings).consensus_signals()
        return {"items": items}

    # ------------------------------------------------------------------
    # Phase 2: smart-money scoring + signals + risk
    # ------------------------------------------------------------------

    @app.get("/api/trader-scores")
    def trader_scores(top: int = 50) -> dict[str, Any]:
        if top < 1 or top > 500:
            raise HTTPException(status_code=400, detail="top must be 1..500")
        from .models import TraderScore

        with _session() as session:
            rows = (
                session.execute(
                    select(TraderScore)
                    .order_by(TraderScore.rank.asc())
                    .limit(top)
                )
                .scalars()
                .all()
            )
            items = [
                {
                    "rank": r.rank,
                    "wallet": _fmt_wallet(r.wallet),
                    "wallet_full": r.wallet,
                    "username": r.username or r.pseudonym or "—",
                    "verified": r.verified,
                    "smart_money_score": r.smart_money_score,
                    "win_rate": r.win_rate,
                    "roi_pct": r.roi_pct,
                    "realized_pnl": r.realized_pnl,
                    "unrealized_pnl": r.unrealized_pnl,
                    "total_pnl": r.total_pnl,
                    "total_volume": r.total_volume,
                    "trade_count": r.trade_count,
                    "closed_count": r.closed_count,
                    "open_count": r.open_count,
                    "avg_hold_hours": r.avg_hold_hours,
                    "max_drawdown": r.max_drawdown,
                    "yes_ratio": r.yes_ratio,
                    "top_category": r.top_category,
                    "computed_at": r.computed_at.isoformat() if r.computed_at else None,
                }
                for r in rows
            ]
        return {"items": items, "scored_at": rows[0].computed_at.isoformat() if rows else None}

    @app.get("/api/trader-scores/window")
    def trader_scores_window(top: int = 50, window_days: int = 30) -> dict[str, Any]:
        """30-day (or other window) profile — what the **"Trader Scores ·
        智能资金画像"** panel renders.

        Sort: ``smart_window_score DESC``, then ``total_pnl DESC``.

        Each item exposes the same fields as the lifetime endpoint plus
        the window-specific ones:

        * ``window_days`` — number of days the profile covers
        * ``days_active`` — distinct trading days in the window
        * ``days_since_active`` — now - last trade
        * ``open_position_count`` — number of live ``CurrentPosition`` rows
        * ``avg_daily_pnl`` — total_pnl / days_active
        * ``smart_window_score`` — composite [0, 100]
        """
        if top < 1 or top > 500:
            raise HTTPException(status_code=400, detail="top must be 1..500")
        if window_days not in (7, 14, 30, 60, 90):
            raise HTTPException(status_code=400, detail="window_days must be 7/14/30/60/90")
        from .models import WindowScore
        from .window_scoring import _compute_window_scores, recompute_window_scores

        with _session() as session:
            # Pick from existing rows when present; otherwise recompute
            # on-the-fly for the requested window.  The persisted table
            # is keyed on window_days so multiple windows can coexist.
            from sqlalchemy import func as _sa_func
            n = (
                session.execute(
                    select(_sa_func.count(WindowScore.wallet)).where(
                        WindowScore.window_days == window_days
                    )
                ).scalar()
                or 0
            )
            if n == 0 and window_days == 30:
                # Common case — fall back to instant recompute.
                recompute_window_scores(session, window_days=window_days)
            elif n == 0:
                # Other windows: on-the-fly but not persisted.
                profiles = _compute_window_scores(session, window_days=window_days)
                rows = [type("Row", (), p) for p in profiles[:top]]
                items = [
                    {
                        "rank": r.rank,
                        "wallet": _fmt_wallet(r.wallet),
                        "wallet_full": r.wallet,
                        "username": r.username or r.pseudonym or "—",
                        "verified": r.verified,
                        "smart_window_score": r.smart_window_score,
                        "win_rate": r.win_rate,
                        "roi_pct": r.roi_pct,
                        "realized_pnl": r.realized_pnl,
                        "unrealized_pnl": r.unrealized_pnl,
                        "total_pnl": r.total_pnl,
                        "total_volume": r.total_volume,
                        "trade_count": r.trade_count,
                        "closed_count": r.closed_count,
                        "open_position_count": r.open_position_count,
                        "days_active": r.days_active,
                        "days_since_active": r.days_since_active,
                        "avg_daily_pnl": r.avg_daily_pnl,
                        "last_active_at": (
                            r.last_active_at.isoformat() if r.last_active_at else None
                        ),
                        "earliest_trade_at": (
                            r.earliest_trade_at.isoformat() if r.earliest_trade_at else None
                        ),
                        "period_days": r.period_days,
                        "profit_days": r.profit_days,
                        "loss_days": r.loss_days,
                        "longest_win_streak": r.longest_win_streak,
                        "max_drawdown_pct": r.max_drawdown_pct,
                        "daily_pnl_stddev": r.daily_pnl_stddev,
                        "top_category": r.top_category,
                        "window_days": r.window_days,
                    }
                    for r in rows
                ]
                return {"items": items, "window_days": window_days, "scored_at": None}

            rows = (
                session.execute(
                    select(WindowScore)
                    .where(WindowScore.window_days == window_days)
                    .order_by(WindowScore.rank.asc())
                    .limit(top)
                )
                .scalars()
                .all()
            )
            items = [
                {
                    "rank": r.rank,
                    "wallet": _fmt_wallet(r.wallet),
                    "wallet_full": r.wallet,
                    "username": r.username or r.pseudonym or "—",
                    "verified": r.verified,
                    "smart_window_score": r.smart_window_score,
                    "win_rate": r.win_rate,
                    "roi_pct": r.roi_pct,
                    "realized_pnl": r.realized_pnl,
                    "unrealized_pnl": r.unrealized_pnl,
                    "total_pnl": r.total_pnl,
                    "total_volume": r.total_volume,
                    "trade_count": r.trade_count,
                    "closed_count": r.closed_count,
                    "open_position_count": r.open_position_count,
                    "days_active": r.days_active,
                    "days_since_active": r.days_since_active,
                    "avg_daily_pnl": r.avg_daily_pnl,
                    "last_active_at": r.last_active_at.isoformat() if r.last_active_at else None,
                    "earliest_trade_at": r.earliest_trade_at.isoformat() if r.earliest_trade_at else None,
                    "period_days": float(r.period_days or 0),
                    "profit_days": int(r.profit_days or 0),
                    "loss_days": int(r.loss_days or 0),
                    "longest_win_streak": int(r.longest_win_streak or 0),
                    "max_drawdown_pct": float(r.max_drawdown_pct or 0),
                    "daily_pnl_stddev": float(r.daily_pnl_stddev or 0),
                    "top_category": r.top_category,
                    "window_days": r.window_days,
                    "computed_at": r.computed_at.isoformat() if r.computed_at else None,
                }
                for r in rows
            ]
        return {
            "items": items,
            "window_days": window_days,
            "scored_at": rows[0].computed_at.isoformat() if rows else None,
        }

    @app.get("/api/signals")
    def signals(
        status: str | None = None,
        signal_type: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        if limit < 1 or limit > 200:
            raise HTTPException(status_code=400, detail="limit must be 1..200")
        from .models import Signal

        with _session() as session:
            stmt = select(Signal).order_by(Signal.created_at.desc())
            if status:
                # Accept any case; persist uses lower-case.
                stmt = stmt.where(Signal.status == status.lower())
            if signal_type:
                stmt = stmt.where(Signal.signal_type == signal_type.lower())
            stmt = stmt.limit(limit)
            rows = session.execute(stmt).scalars().all()
            items = [
                {
                    "id": r.id,
                    "signal_type": r.signal_type,
                    "status": r.status,
                    "condition_id": r.condition_id,
                    "direction": r.direction,
                    "outcome": r.outcome_label,
                    "trader_count": r.trader_count,
                    "total_value": r.total_value,
                    "avg_entry_price": r.avg_entry_price,
                    "current_price": r.current_price,
                    "confidence": r.confidence,
                    "suggested_size_usdc": r.suggested_size_usdc,
                    "risk_reasons": r.risk_reasons,
                    "trigger_wallets": r.trigger_wallets,
                    "title": r.title,
                    "slug": r.slug,
                    "category": r.category,
                    "end_time": r.end_time.isoformat() if r.end_time else None,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                }
                for r in rows
            ]
        return {"items": items, "count": len(items)}

    @app.get("/api/signals/stats")
    def signal_stats() -> dict[str, Any]:
        from sqlalchemy import func
        from .models import Signal

        with _session() as session:
            rows = session.execute(
                select(Signal.status, func.count(Signal.id)).group_by(Signal.status)
            ).all()
            by_status = {row[0]: row[1] for row in rows}
            types = session.execute(
                select(Signal.signal_type, func.count(Signal.id)).group_by(Signal.signal_type)
            ).all()
            by_type = {row[0]: row[1] for row in types}
        return {
            "by_status": by_status,
            "by_type": by_type,
            "pass_count": by_status.get("pass", 0),
            "shrink_count": by_status.get("shrink", 0),
            "block_count": by_status.get("block", 0),
            "pending_count": by_status.get("pending", 0),
        }

    # ------------------------------------------------------------------
    # Follow list + follow orders (Phase 3: copy-trading executor)
    # ------------------------------------------------------------------

    @app.get("/api/follow-list")
    def follow_list(top: int = 50) -> dict[str, Any]:
        from .followlist import get_follow_list

        with _session() as session:
            items = get_follow_list(session, top_n=top)
        return {"items": items, "count": len(items)}

    @app.get("/api/follow-candidates")
    def follow_candidates(top: int = 50) -> dict[str, Any]:
        """Debug-only: show the candidates that *didn't* make the follow list
        and the specific threshold(s) each one failed.

        The follow-loop populates ``session.info['follow_last_run']`` on each
        tick.  Since the API runs in a separate process from the loop, we
        persist that snapshot to a temp file so this endpoint always has
        fresh data.
        """
        from .followlist import _read_follow_last_run  # local import
        snap = _read_follow_last_run()
        if not snap:
            return {"items": [], "count": 0, "snapshot": None,
                    "hint": "follow-list 还没运行过；请等待 30 秒或 POST /api/follow/refresh"}
        items = snap.get("rejected", [])[:top]
        return {
            "items": items,
            "count": len(items),
            "snapshot": {
                "generated_at": snap.get("generated_at"),
                "thresholds": snap.get("thresholds"),
                "totals": snap.get("totals"),
            },
        }

    @app.get("/api/follow-orders")
    def follow_orders(
        limit: int = 50,
        include_dry_run: bool | None = None,
        follow_list_only: bool = True,
        sort: str = "time",
        order: str = "desc",
    ) -> dict[str, Any]:
        """Audit log of *live* copy-trade orders the operator can act on.

        The previous version returned *every* FollowOrder row, including
        rows for wallets the follow-list had since dropped and rows for
        conditions we no longer hold.  The operator's complaint: the
        "执行日志" tab was noisy with old / irrelevant rows, making it
        hard to find orders that actually need attention.  This version
        applies the *intersection* the user asked for:

          FollowOrder.wallet ∈ FollowListEntry       (still a top trader)
          AND
          ∃ CurrentPosition(wallet, condition_id)   (we currently hold it)

        Both can be disabled with ``?follow_list_only=0`` to fall back
        to the legacy "all live orders" view.

        ``sort`` supports: ``time`` (default), ``probability`` (uses
        ``FollowOrder.price``), ``pnl`` (uses
        ``CurrentPosition.percent_pnl``), ``size``.  ``order`` is
        ``desc`` (default) or ``asc``.
        """
        from .models import CurrentPosition, FollowListEntry, FollowOrder, Market, Signal

        if limit < 1 or limit > 500:
            raise HTTPException(status_code=400, detail="limit must be 1..500")
        live_only = include_dry_run is None or not include_dry_run
        sort = (sort or "time").lower()
        order = (order or "desc").lower()
        if sort not in {"time", "probability", "pnl", "size"}:
            raise HTTPException(status_code=400, detail="sort must be one of time|probability|pnl|size")
        if order not in {"asc", "desc"}:
            raise HTTPException(status_code=400, detail="order must be asc|desc")

        with _session() as session:
            # Live-only filter: only orders that are still "in flight"
            # (pending / submitted / inflight) or settled (filled) or
            # were skipped / failed.  Dry-run rows are excluded unless
            # the caller asked for them.
            q = session.query(FollowOrder)
            if live_only:
                q = q.filter(
                    FollowOrder.status.in_(
                        ["pending", "submitted", "error", "filled", "failed", "skipped", "inflight"]
                    )
                )
            if follow_list_only:
                # Restrict to wallets currently on the follow-list (auto
                # or manual pin).  Subquery: a single ``IN`` clause over
                # FollowListEntry.wallet keeps this fast.
                follow_wallets = [
                    w for (w,) in session.query(FollowListEntry.wallet).all()
                ]
                # An empty follow-list should still show operator-curated
                # manual orders (signal_id=0), so we add "manual" as a
                # pseudo-wallet for backwards-compat with rows the
                # operator placed from the dashboard.
                if "manual" not in follow_wallets:
                    follow_wallets.append("manual")
                q = q.filter(FollowOrder.wallet.in_(follow_wallets))

            # Sort.  ``time`` is the default since that matches the
            # leftmost column the operator reads first.  ``pnl`` and
            # ``probability`` need CurrentPosition to compute, so we
            # build the join in those cases; the LEFT OUTER JOIN
            # guarantees pending orders (no position yet) still appear.
            if sort in {"pnl", "probability"}:
                pos_sub = (
                    session.query(
                        CurrentPosition.wallet.label("pw"),
                        CurrentPosition.condition_id.label("pc"),
                        CurrentPosition.current_price.label("cprice"),
                        CurrentPosition.avg_price.label("aprice"),
                        CurrentPosition.percent_pnl.label("pct"),
                    )
                ).subquery()
                q = q.outerjoin(
                    pos_sub,
                    (pos_sub.c.pw == FollowOrder.wallet) & (pos_sub.c.pc == FollowOrder.condition_id),
                )
                if sort == "pnl":
                    sort_col = pos_sub.c.pct
                else:
                    sort_col = pos_sub.c.cprice
            elif sort == "size":
                sort_col = FollowOrder.size_usdc
            else:  # time
                sort_col = FollowOrder.created_at

            q = q.order_by(sort_col.asc() if order == "asc" else sort_col.desc())
            rows = q.limit(limit).all()

            # Enrich with Market slug + title, plus CurrentPosition
            # (for current price / PnL).  We do this in two passes so
            # the second query only touches the rows we actually
            # returned (no full-table scans).
            cond_ids = {r.condition_id for r in rows if r.condition_id}
            wallet_cond_pairs = {(r.wallet, r.condition_id) for r in rows}
            slug_by_cond: dict[str, dict[str, Any]] = {}
            if cond_ids:
                for cid, slug, event_slug, title in session.query(
                    Market.condition_id, Market.slug, Market.event_slug, Market.question
                ).filter(Market.condition_id.in_(cond_ids)).all():
                    slug_by_cond[cid] = {
                        "slug": slug,
                        "event_slug": event_slug,
                        "title": title,
                    }
                # Fallback: orphan conditions where Market cache has no
                # row yet — derive slug/title from the Signal table.
                missing = cond_ids - slug_by_cond.keys()
                if missing:
                    sig_rows = session.query(
                        Signal.condition_id, Signal.slug, Signal.title
                    ).filter(Signal.condition_id.in_(missing)).order_by(
                        Signal.id.desc()
                    ).all()
                    for cid, slug, title in sig_rows:
                        if cid in slug_by_cond:
                            continue
                        slug_by_cond[cid] = {
                            "slug": slug,
                            "event_slug": None,
                            "title": title,
                        }

            pos_by_key: dict[tuple[str, str], dict[str, Any]] = {}
            if wallet_cond_pairs:
                wallets = {p[0] for p in wallet_cond_pairs}
                conds = {p[1] for p in wallet_cond_pairs}
                for w, c, cprice, aprice, pct, current_value, cash_pnl, size in session.query(
                    CurrentPosition.wallet,
                    CurrentPosition.condition_id,
                    CurrentPosition.current_price,
                    CurrentPosition.avg_price,
                    CurrentPosition.percent_pnl,
                    CurrentPosition.current_value,
                    CurrentPosition.cash_pnl,
                    CurrentPosition.size,
                ).filter(
                    CurrentPosition.wallet.in_(wallets),
                    CurrentPosition.condition_id.in_(conds),
                    CurrentPosition.size > 0,
                ).all():
                    pos_by_key[(w, c)] = {
                        "current_price": float(cprice or 0),
                        "avg_price": float(aprice or 0),
                        "percent_pnl": float(pct or 0),
                        "current_value": float(current_value or 0),
                        "cash_pnl": float(cash_pnl or 0),
                        "size": float(size or 0),
                    }

            items = []
            for r in rows:
                mkt = slug_by_cond.get(r.condition_id) or {}
                pos = pos_by_key.get((r.wallet, r.condition_id)) or {}
                # Current probability = best of (CLOB current_price,
                # order.price).  When the order hasn't filled yet, the
                # only "current" number we have is the order's limit
                # price — surface that so the operator can still see
                # what they were willing to pay.
                order_price = float(r.price or 0)
                current_price = float(pos.get("current_price") or 0)
                pnl_pct = float(pos.get("percent_pnl") or 0)
                if current_price > 0 and order_price > 0:
                    # Use (current - entry) / entry for the move, which
                    # works regardless of side: a YES position gains
                    # when current_price > entry, NO position gains
                    # when current_price < entry.
                    pnl_pct = (current_price - order_price) / order_price * 100
                items.append({
                    "id": r.id,
                    "signal_id": r.signal_id,
                    "wallet": r.wallet,
                    "token_id": r.token_id,
                    "condition_id": r.condition_id,
                    "slug": mkt.get("slug"),
                    "event_slug": mkt.get("event_slug"),
                    "title": mkt.get("title"),
                    "direction": r.direction,
                    "side": r.side,
                    "price": order_price,            # entry price
                    "current_price": current_price,  # CLOB mark
                    "size_usdc": r.size_usdc,
                    "size_shares": pos.get("size"),
                    "pnl_pct": pnl_pct,
                    "cash_pnl": pos.get("cash_pnl"),
                    "current_value": pos.get("current_value"),
                    "status": r.status,
                    "note": (r.note or "")[:240],
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "in_follow_list": True,  # by construction
                    "has_position": bool(pos),
                })
        return {
            "items": items,
            "count": len(items),
            "live_only": live_only,
            "follow_list_only": follow_list_only,
            "sort": sort,
            "order": order,
        }

    # ------------------------------------------------------------------
    # 执行日志 · 跟单 orders —— rebuilt as a *positions snapshot*.
    # The original FollowOrder audit log was full of dry_run / pending /
    # error / skipped rows that no longer had anything to do with the
    # operator's actual positions.  The user asked for one row per
    # (钱包, 事件) — the rows that are **still being held** by a wallet
    # currently on the follow-list.  Anything without an active
    # ``CurrentPosition`` is hidden.
    # ------------------------------------------------------------------
    @app.get("/api/follow-positions")
    def follow_positions(
        sort: str = "probability",
        order: str = "desc",
        limit: int = 100,
        offset: int = 0,
        max_age_days: float = 3.0,
    ) -> dict[str, Any]:
        """Return the live-positions view.

        Primary key = (wallet, condition_id).  Only rows where the
        wallet is in :class:`FollowListEntry` *and* there is a
        ``CurrentPosition`` with size > 0 are returned — that's the
        strict definition of "仍在持仓且生效中".

        ``sort`` supports:
          - ``probability``  (default; uses ``current_price`` desc)
          - ``pnl``          (uses ``percent_pnl``)
          - ``value``        (uses ``current_value``)
          - ``size``         (uses ``size`` shares)
          - ``age``          (uses ``original_created_at`` — the actual
            time the leader first opened the position, derived from
            MIN(traded_at) for BUYs; falls back to
            ``first_observed_at`` if no trade row exists yet).
            ``age:desc`` → newest positions first, ``age:asc`` →
            oldest positions first.

        ``order`` is ``desc`` (default) or ``asc``.

        ``max_age_days`` (default 3) auto-filters out positions whose
        age is older than the cutoff.  Pass a negative value (e.g.
        ``-1``) to disable the filter.  ``age`` is computed the same
        way as for the ``age`` sort key — i.e. via ``buy_open`` first,
        then ``first_observed_at`` as fallback.  Positions whose age
        timestamp is missing are kept (we can't tell if they should be
        filtered, so we err on the side of showing them).

        ``offset`` + ``limit`` paginate.  The response carries both
        ``count`` (rows on this page) and ``total`` (rows in the
        filtered set, before pagination), so the frontend can render
        page controls without an extra count query.
        """
        from .models import CurrentPosition, FollowListEntry, Trade

        if limit < 1 or limit > 500:
            raise HTTPException(status_code=400, detail="limit must be 1..500")
        if offset < 0:
            raise HTTPException(status_code=400, detail="offset must be ≥ 0")
        sort = (sort or "probability").lower()
        order = (order or "desc").lower()
        if sort not in {"probability", "pnl", "value", "size", "age"}:
            raise HTTPException(
                status_code=400,
                detail="sort must be one of probability|pnl|value|size|age",
            )
        if order not in {"asc", "desc"}:
            raise HTTPException(status_code=400, detail="order must be asc|desc")

        with _session() as session:
            # Strict intersection: wallet ∈ FollowListEntry AND
            # CurrentPosition.size > 0.  Manual follow-list entries
            # count too (they share the same table).
            follow_wallets = {
                w: (u, p)
                for w, u, p in session.query(
                    FollowListEntry.wallet,
                    FollowListEntry.username,
                    FollowListEntry.pseudonym,
                ).all()
            }
            if not follow_wallets:
                return {
                    "items": [],
                    "count": 0,
                    "sort": sort,
                    "order": order,
                }

            pos_q = (
                session.query(CurrentPosition)
                .filter(
                    CurrentPosition.wallet.in_(follow_wallets.keys()),
                    CurrentPosition.size > 0,
                )
            )
            rows = pos_q.all()

            # Pull the earliest BUY timestamp per (wallet, condition_id,
            # token_id) from the trades table in one shot.  This is the
            # "true" position creation time — when the leader first
            # opened the position on Polymarket.  Frontend falls back to
            # ``first_observed_at`` (when our collector first noticed
            # it) if no trade row exists for that key.
            #
            # We key on (wallet, condition_id, token_id) — token_id
            # uniquely distinguishes YES vs NO under the same
            # conditionId.  A LEFT JOIN semantics handles the
            # "no trades yet" case by returning NULL.
            buy_open = {}
            if rows:
                keys = [(r.wallet, r.condition_id, r.token_id) for r in rows]
                q = (
                    session.query(
                        Trade.wallet,
                        Trade.condition_id,
                        Trade.token_id,
                        func.min(Trade.traded_at).label("first_trade_at"),
                    )
                    .filter(
                        Trade.side == "BUY",
                        Trade.wallet.in_({k[0] for k in keys}),
                        Trade.condition_id.in_({k[1] for k in keys}),
                    )
                    .group_by(Trade.wallet, Trade.condition_id, Trade.token_id)
                )
                for w, c, t, first_trade_at in q.all():
                    buy_open[(w, c, t)] = first_trade_at

            # ``_age_key`` is the single source of truth for both the
            # ``age`` sort and the ``max_age_days`` filter — and it
            # MUST match ``original_created_at`` in the response so a
            # row that displays "持仓已存在: 5d" cannot simultaneously
            # be invisible to the 3-day filter.  We use the strict
            # value (only chain-trade timestamps); rows without one
            # sort LAST in either order, and rows without one are
            # EXCLUDED by the max_age_days filter (we cannot prove
            # they are < 3 days old — better to drop than mislabel).
            def _age_key(r):
                return buy_open.get((r.wallet, r.condition_id, r.token_id))

            if sort == "age":
                rows.sort(
                    key=lambda r: (_age_key(r) is None, _age_key(r)),
                    reverse=(order == "desc"),
                )
            else:
                sort_attr = {
                    "probability": "current_price",
                    "pnl": "percent_pnl",
                    "value": "current_value",
                    "size": "size",
                }[sort]
                rows.sort(
                    key=lambda r: float(getattr(r, sort_attr) or 0),
                    reverse=(order == "desc"),
                )

            # Auto-hide positions older than ``max_age_days``.  Cutoff
            # compared against the strict ``_age_key`` (chain trade
            # timestamp); rows with no chain evidence are dropped
            # because we can't vouch for their age.  Pass
            # ``max_age_days`` < 0 to disable the filter entirely.
            if max_age_days is not None and max_age_days >= 0:
                cutoff = utc_now() - timedelta(days=max_age_days)
                rows = [r for r in rows if _age_key(r) and _age_key(r) >= cutoff]

            total = len(rows)
            page_rows = rows[offset : offset + limit]

            items: list[dict[str, Any]] = []
            for r in page_rows:
                username, pseudonym = follow_wallets.get(r.wallet, (None, None))
                avg = float(r.avg_price or 0)
                cur = float(r.current_price or 0)
                size_shares = float(r.size or 0)
                pct = float(r.percent_pnl or 0)
                value = float(r.current_value or 0)
                # Single-position delta: (current - entry)/entry × 100.
                # Works regardless of YES/NO side because avg_price is
                # already the share-cost basis the leader paid.
                delta = ((cur - avg) / avg * 100) if (avg > 0 and cur > 0) else pct
                direction = (r.outcome or "").upper() or "YES"
                # Original creation time — STRICT.
                #   Only set when we have actual chain evidence
                #   (MIN(traded_at) from the trades table for the
                #   wallet's BUY on this condition/token).  We
                #   deliberately do NOT fall back to
                #   ``first_observed_at`` or ``observed_at`` — those
                #   equal "now" for freshly discovered positions and
                #   would falsely display the position as "持仓已存在:
                #   几分钟".  The frontend will render this as "—".
                #   Trade collection runs every few minutes; once it
                #   catches up, the cell populates automatically.
                first_trade_at = buy_open.get((r.wallet, r.condition_id, r.token_id))
                items.append({
                    "wallet": r.wallet,
                    "username": username,
                    "pseudonym": pseudonym,
                    "condition_id": r.condition_id,
                    "token_id": r.token_id,
                    "slug": r.slug,
                    "event_slug": r.event_slug,
                    "title": r.title,
                    "direction": direction,
                    "outcome": r.outcome,
                    "avg_price": avg,            # entry
                    "current_price": cur,        # probability / mark
                    "size_shares": size_shares,
                    "current_value": value,
                    "cash_pnl": float(r.cash_pnl or 0),
                    "percent_pnl": pct,
                    "delta_pct": delta,
                    "first_observed_at": r.first_observed_at.isoformat() if r.first_observed_at else None,
                    "observed_at": r.observed_at.isoformat() if r.observed_at else None,
                    # Strict original creation timestamp.  Null when
                    # trade history hasn't been collected for this
                    # (wallet, condition_id, token_id) yet — see comment
                    # above.
                    "original_created_at": first_trade_at.isoformat() if first_trade_at else None,
                    "original_created_source": "trade" if first_trade_at else None,
                })
        return {
            "items": items,
            "count": len(items),
            "total": total,            # rows after max_age_days filter
            "offset": offset,
            "limit": limit,
            "max_age_days": max_age_days,
            "sort": sort,
            "order": order,
        }

    @app.post("/api/follow/refresh")
    def follow_refresh() -> dict[str, Any]:
        """Manually trigger follow-list recompute + follow-tick (dry-run by default)."""
        from .followlist import refresh_follow_list, get_follow_list
        from .executor import execute as exec_order
        from .models import FollowOrder, Signal

        with _session() as session:
            top = refresh_follow_list(session, settings)
            items = get_follow_list(session, top_n=settings.follow_list_max)
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
            # Process any pending pass signals (capped to last 5min, same as the loop tick)
            sigs = (
                session.query(Signal)
                .filter(
                    Signal.status == "pass",
                    Signal.signal_type == "consensus",
                    Signal.trader_count >= settings.follow_min_consensus_for_execute,
                    Signal.created_at >= cutoff,
                )
                .order_by(Signal.created_at.desc())
                .limit(20)
                .all()
            )
            results = []
            for s in sigs:
                existing = session.query(FollowOrder).filter(FollowOrder.signal_id == s.id).first()
                if existing:
                    continue
                r = exec_order(session, settings, signal_id=s.id)
                results.append(r)
            session.commit()
        return {
            "follow_list_size": len(items),
            "top_wallets": top,
            "executed_signals": results,
        }

    # ------------------------------------------------------------------
    # Manual follow — operator-curated wallet additions (Phase 5)
    # ------------------------------------------------------------------

    @app.get("/api/follow-manual")
    def follow_manual_list() -> dict[str, Any]:
        """List every wallet the operator has manually pinned to the follow list."""
        from .models import ManualFollow, WindowScore
        with _session() as session:
            rows = session.execute(select(ManualFollow)).scalars().all()
            items = []
            for m in rows:
                ws = session.execute(
                    select(WindowScore).where(
                        WindowScore.wallet == m.wallet,
                        WindowScore.window_days == 30,
                    )
                ).scalar_one_or_none()
                items.append({
                    "wallet": m.wallet,
                    "username": m.username,
                    "note": m.note,
                    "added_at": m.added_at.isoformat() if m.added_at else None,
                    "last_active_at": ws.last_active_at.isoformat() if ws and ws.last_active_at else None,
                    "days_since_active": float(ws.days_since_active) if ws and ws.days_since_active is not None else 999.0,
                    "open_position_count": int(ws.open_position_count) if ws else 0,
                    "roi_pct": float(ws.roi_pct) if ws else 0.0,
                    "realized_pnl": float(ws.realized_pnl) if ws else 0.0,
                    "loss_days": int(ws.loss_days) if ws else None,
                    "max_drawdown_pct": float(ws.max_drawdown_pct) if ws else None,
                    "in_follow_list": True,
                })
        return {"items": items, "count": len(items)}

    @app.post("/api/follow-manual/add")
    def follow_manual_add(payload: dict[str, Any]) -> dict[str, Any]:
        """Pin a wallet to the follow list.  Bypasses PnL / loss_days /
        drawdown gates; recency is still enforced on every follow tick.

        Body: ``{"wallet": "0x...", "username": "...", "note": "..."}``.
        ``wallet`` is required; ``username`` and ``note`` are optional.
        After insert we call ``refresh_follow_list`` so the manual
        entry shows up immediately.
        """
        from .followlist import refresh_follow_list, get_follow_list
        from .models import ManualFollow

        wallet = (payload.get("wallet") or "").strip().lower()
        if not wallet or not wallet.startswith("0x") or not re.fullmatch(r"0x[0-9a-f]{40}", wallet):
            raise HTTPException(
                status_code=400,
                detail="wallet must be a 0x-prefixed 40-char hex address (e.g. 0x204f72f35326db932158cba6adff0b9a1da95e14)",
            )
        username = (payload.get("username") or "").strip() or None
        note = (payload.get("note") or "").strip() or None

        with _session() as session:
            existing = session.get(ManualFollow, wallet)
            if existing:
                if username:
                    existing.username = username
                if note:
                    existing.note = note
            else:
                session.add(ManualFollow(wallet=wallet, username=username, note=note))
            # Manual-pinned wallets are operator-curated: make sure the
            # Trader row exists with tracked=True so the per-tick
            # trades/positions collectors include it even if it has not yet
            # appeared on a public leaderboard.
            from .models import Trader
            trader = session.get(Trader, wallet)
            if trader is None:
                session.add(Trader(
                    wallet=wallet,
                    username=username,
                    tracked=True,
                    first_seen_at=datetime.now(timezone.utc),
                    last_seen_at=datetime.now(timezone.utc),
                ))
            elif not trader.tracked:
                trader.tracked = True
            if username and trader is not None and not trader.username:
                trader.username = username
            session.commit()
            refresh_follow_list(session, settings)
            session.commit()
            items = get_follow_list(session, top_n=settings.follow_list_max)
        return {"ok": True, "wallet": wallet, "follow_list_size": len(items)}

    @app.post("/api/follow-manual/remove")
    def follow_manual_remove(payload: dict[str, Any]) -> dict[str, Any]:
        """Un-pin a wallet from the manual-follow table.

        Body: ``{"wallet": "0x..."}``.
        """
        from .followlist import refresh_follow_list, get_follow_list
        from .models import ManualFollow

        wallet = (payload.get("wallet") or "").strip().lower()
        if not wallet:
            raise HTTPException(status_code=400, detail="wallet required")
        with _session() as session:
            existing = session.get(ManualFollow, wallet)
            if existing:
                session.delete(existing)
                session.commit()
            refresh_follow_list(session, settings)
            session.commit()
            items = get_follow_list(session, top_n=settings.follow_list_max)
        return {"ok": True, "wallet": wallet, "follow_list_size": len(items)}

    # ------------------------------------------------------------------
    # Phase 4: manual order entry from the trading tab + leader-sales
    # detector ("leader already sold while we are still in")
    # ------------------------------------------------------------------

    @app.post("/api/follow/orders/{order_db_id}/approve")
    def approve_pending_order(order_db_id: int) -> dict[str, Any]:
        """Approve a pending semi-auto order and submit it for real.

        The executor writes ``FollowOrder(status='pending', note='SEMI-AUTO
        approval required: …')`` when ``settings.live_trade=True`` and
        ``settings.semi_auto=True`` and a consensus signal fires.  This
        endpoint flips that row to ``submitted`` (or ``error`` if the CLOB
        call fails) by re-running ``_submit_live`` against the stored plan.

        Idempotency: a second call to the same ``order_db_id`` returns the
        already-known outcome — we don't fire a second CLOB order.
        """
        from .executor import _submit_live
        from .models import FollowOrder
        from api import (
            OrderEvent,
            OrderEventBus,
            OrderStatus,
            get_order_event_bus,
        )

        with _session() as session:
            order = session.get(FollowOrder, order_db_id)
            if order is None:
                raise HTTPException(status_code=404, detail=f"order {order_db_id} not found")
            if order.status not in ("pending",):
                return {
                    "ok": order.status in ("submitted", "filled"),
                    "order_id": order.id,
                    "status": order.status,
                    "note": order.note,
                    "idempotent": True,
                }

            plan = _OrderPlanForApproval(
                token_id=order.token_id,
                price=order.price,
                size_usdc=order.size_usdc,
                size_shares=round(order.size_usdc / order.price, 2) if order.price else 0.0,
            )
            bus = get_order_event_bus()
            clob_order_id = f"ct-{order.wallet[:8]}-{order.condition_id[:8]}-{order.id}"
            bus.publish(OrderEvent(
                event_id="",
                order_id=clob_order_id,
                leader_wallet=order.wallet,
                market_id=order.condition_id,
                asset_id=order.token_id,
                side=order.side,
                status=OrderStatus.INFLIGHT,
                reason="human approved pending order",
                data={
                    "source": "operator",
                    "entry_price": float(order.price or 0),
                    "follow_order_db_id": order.id,
                },
            ))

            ok, msg = _submit_live(
                _build_plan_from_row(order, settings), settings
            )
            new_status = "submitted" if ok else "error"
            order.status = new_status
            order.note = (order.note or "") + f" | APPROVED: {msg}"
            session.commit()

            bus.publish(OrderEvent(
                event_id="",
                order_id=clob_order_id,
                leader_wallet=order.wallet,
                market_id=order.condition_id,
                asset_id=order.token_id,
                side=order.side,
                status=OrderStatus.FILLED if ok else OrderStatus.FAILED,
                reason=msg if not ok else None,
                data={
                    "clob_message": msg,
                    "source": "operator",
                    "entry_price": float(order.price or 0),
                    "follow_order_db_id": order.id,
                },
            ))

            return {
                "ok": ok,
                "order_id": order.id,
                "status": new_status,
                "note": msg,
                "idempotent": False,
            }

    @app.post("/api/follow/orders/{order_db_id}/cancel")
    def cancel_pending_order(order_db_id: int) -> dict[str, Any]:
        """Cancel a pending semi-auto order before it ever hits the CLOB.

        Marks the row as ``skipped`` with a clear note.  Idempotent — a
        second call is a no-op.
        """
        from .models import FollowOrder

        with _session() as session:
            order = session.get(FollowOrder, order_db_id)
            if order is None:
                raise HTTPException(status_code=404, detail=f"order {order_db_id} not found")
            if order.status != "pending":
                return {
                    "ok": True,
                    "order_id": order.id,
                    "status": order.status,
                    "idempotent": True,
                    "note": "order not in pending state, no-op",
                }
            order.status = "skipped"
            order.note = (order.note or "") + " | CANCELLED by operator"
            session.commit()
            return {
                "ok": True,
                "order_id": order.id,
                "status": order.status,
                "idempotent": False,
            }

    @app.get("/api/me/positions")
    def me_positions(limit: int = 100) -> dict[str, Any]:
        """Open positions held by the operator's own personal account.

        Returns one row per (condition_id, direction) where a
        ``FollowOrder`` with ``signal_id=0`` (operator-curated, never
        signal-driven) is in an open / settled state
        (``submitted`` / ``filled`` / ``inflight``).  This is the only
        way to get a non-leader view — every other "follow orders"
        endpoint joins against ``FollowListEntry``.

        The renderer on the dashboard pairs each row with a "抛售"
        button that hits ``POST /api/me/positions/{id}/sell``.  The
        backend derives the SELL side from the BUY direction.
        """
        from .models import CurrentPosition, FollowOrder, Market

        if limit < 1 or limit > 500:
            raise HTTPException(status_code=400, detail="limit must be 1..500")

        with _session() as session:
            # Operator's manual BUY orders currently open.  We only
            # show BUY rows because a SELL is by construction the
            # exit — once it lands the row is no longer "open".
            open_buys = (
                session.query(FollowOrder)
                .filter(
                    FollowOrder.signal_id == 0,
                    FollowOrder.side == "BUY",
                    FollowOrder.status.in_(("submitted", "filled", "inflight")),
                    FollowOrder.token_id.isnot(None),
                )
                .order_by(FollowOrder.created_at.desc())
                .limit(limit)
                .all()
            )
            if not open_buys:
                return {"items": [], "count": 0, "funder": settings.polymarket_funder}

            cond_ids = {o.condition_id for o in open_buys if o.condition_id}
            pos_by_key: dict[tuple[str, str], dict[str, Any]] = {}
            for w, c, cur, sz, ap, end_t, ttl, slug, ev_slug in session.query(
                CurrentPosition.wallet,
                CurrentPosition.condition_id,
                CurrentPosition.current_price,
                CurrentPosition.size,
                CurrentPosition.avg_price,
                CurrentPosition.end_time,
                CurrentPosition.title,
                CurrentPosition.slug,
                CurrentPosition.event_slug,
            ).filter(
                CurrentPosition.condition_id.in_(cond_ids),
                CurrentPosition.size > 0,
            ).all():
                # ``(condition_id, outcome)`` is the natural key for an
                # open position — outcome is the YES/NO direction.
                pos_by_key[(c, w)] = {  # w is overloaded — re-bind properly below
                    "current_price": float(cur or 0),
                    "size": float(sz or 0),
                    "avg_price": float(ap or 0),
                    "end_time": end_t,
                    "title": ttl,
                    "slug": slug,
                    "event_slug": ev_slug,
                }

            # The ``outcome`` column (YES/NO) is what ties a BUY to a
            # position; without it we can't disambiguate a YES-BUY from
            # a NO-BUY on the same market.  Refetch with outcome too.
            pos_by_key = {}
            for w, c, oc, cur, sz, ap, end_t, ttl, slug, ev_slug in session.query(
                CurrentPosition.wallet,
                CurrentPosition.condition_id,
                CurrentPosition.outcome,
                CurrentPosition.current_price,
                CurrentPosition.size,
                CurrentPosition.avg_price,
                CurrentPosition.end_time,
                CurrentPosition.title,
                CurrentPosition.slug,
                CurrentPosition.event_slug,
            ).filter(
                CurrentPosition.condition_id.in_(cond_ids),
                CurrentPosition.size > 0,
            ).all():
                pos_by_key[(c, oc)] = {
                    "current_price": float(cur or 0),
                    "size": float(sz or 0),
                    "avg_price": float(ap or 0),
                    "end_time": end_t,
                    "title": ttl,
                    "slug": slug,
                    "event_slug": ev_slug,
                }

            market_meta: dict[str, dict[str, Any]] = {}
            for cid, slug, ev_slug, ttl in session.query(
                Market.condition_id, Market.slug, Market.event_slug, Market.question
            ).filter(Market.condition_id.in_(cond_ids)).all():
                market_meta[cid] = {
                    "slug": slug,
                    "event_slug": ev_slug,
                    "title": ttl,
                }

            items: list[dict[str, Any]] = []
            for o in open_buys:
                pos = pos_by_key.get((o.condition_id, o.direction)) or {}
                meta = market_meta.get(o.condition_id) or {}
                entry_price = float(o.price or 0)
                cur = pos.get("current_price") or entry_price
                shares = round(float(o.size_usdc or 0) / entry_price, 2) if entry_price > 0 else 0.0
                current_value = shares * cur if cur > 0 else 0.0
                cost = shares * entry_price
                pnl = (cur - entry_price) * shares if cur > 0 and entry_price > 0 else 0.0
                pnl_pct = ((cur / entry_price) - 1.0) * 100.0 if entry_price > 0 else 0.0
                items.append({
                    "follow_order_id": o.id,
                    "condition_id": o.condition_id,
                    "asset_id": o.token_id,
                    "direction": o.direction,
                    "side": o.side,
                    "entry_price": entry_price,
                    "current_price": cur,
                    "shares": shares,
                    "cost_usdc": cost,
                    "current_value": current_value,
                    "cash_pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "title": pos.get("title") or meta.get("title") or o.condition_id[:12] + "…",
                    "slug": pos.get("slug") or meta.get("slug"),
                    "event_slug": pos.get("event_slug") or meta.get("event_slug"),
                    "end_time": pos.get("end_time").isoformat() if pos.get("end_time") else None,
                    "status": o.status,
                    "created_at": o.created_at.isoformat() if o.created_at else None,
                })
            return {
                "items": items,
                "count": len(items),
                "funder": settings.polymarket_funder,
            }

    @app.post("/api/me/positions/{follow_order_id}/sell")
    def me_sell_position(
        follow_order_id: int,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Sell (close) one of the operator's own open positions.

        Always SELL — the side argument is hard-coded.  The price is
        the operator-supplied ``price`` from the body, or the latest
        ``CurrentPosition.current_price`` if not supplied.  Size is the
        full position size by default; the operator can override via
        ``size_shares``.

        Idempotency: a second call against the same ``follow_order_id``
        when the row is already ``skipped`` returns the cached note.
        """
        from .executor import _submit_live
        from .models import CurrentPosition, FollowOrder
        from api import (
            OrderEvent,
            OrderEventBus,
            OrderStatus,
            get_order_event_bus,
        )

        payload = payload or {}
        override_price = payload.get("price")
        override_shares = payload.get("size_shares")

        with _session() as session:
            order = session.get(FollowOrder, follow_order_id)
            if order is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"order {follow_order_id} not found",
                )
            # Only operator's *manual* (signal_id=0) BUY rows are
            # eligible.  Anything else is rejected outright — we never
            # close a follow-list wallet's position from this panel.
            if order.signal_id != 0:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"order {follow_order_id} is not a manual BUY "
                        f"(signal_id={order.signal_id}); refusing to sell"
                    ),
                )
            if order.side != "BUY":
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"order {follow_order_id} is not a BUY "
                        f"(side={order.side}); this endpoint is exit-only"
                    ),
                )
            if order.status not in ("submitted", "filled", "inflight"):
                # Idempotent path: caller already sold it.
                return {
                    "ok": order.status in ("submitted", "filled"),
                    "follow_order_id": order.id,
                    "status": order.status,
                    "note": order.note,
                    "idempotent": True,
                }

            # Resolve the live mark from CurrentPosition.  When the
            # positions-tick hasn't run yet for this market the row is
            # missing, in which case we fall back to the BUY's stored
            # price (so the operator sees a sane default in the UI).
            pos = session.query(CurrentPosition).filter(
                CurrentPosition.condition_id == order.condition_id,
                CurrentPosition.outcome == order.direction,
            ).first()
            live_price = (
                float(pos.current_price) if pos and pos.current_price else None
            )
            sell_price = float(override_price) if override_price is not None else (
                live_price or float(order.price or 0)
            )
            if sell_price <= 0 or sell_price >= 1:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "sell price out of range; supply a valid price "
                        "(current_price missing and stored entry price "
                        "is unusable)"
                    ),
                )

            entry_price = float(order.price or 0)
            shares = round(
                float(order.size_usdc or 0) / entry_price, 2
            ) if entry_price > 0 else 0.0
            sell_shares = float(override_shares) if override_shares else shares
            if sell_shares <= 0:
                raise HTTPException(
                    status_code=400,
                    detail="computed sell_shares <= 0; check entry price",
                )

            bus = get_order_event_bus()
            order_id = f"ct-me-sell-{order.condition_id[:8]}-{order.id}"
            bus.publish(OrderEvent(
                event_id="",
                order_id=order_id,
                leader_wallet="me",
                market_id=order.condition_id,
                asset_id=order.token_id,
                side="SELL",
                status=OrderStatus.INFLIGHT,
                reason="operator initiated SELL on personal position",
                data={
                    "source": "operator",
                    "follow_order_id": order.id,
                    "entry_price": entry_price,
                    "current_price": sell_price,
                    "size_shares": sell_shares,
                },
            ))

            ok, msg = _submit_live(
                _build_plan_from_row(order, settings).__class__(
                    signal_id=order.signal_id,
                    wallet=order.wallet,
                    condition_id=order.condition_id,
                    direction=order.direction,
                    token_id=order.token_id,
                    side="SELL",
                    price=sell_price,
                    size_usdc=round(sell_shares * sell_price, 2),
                    size_shares=sell_shares,
                ),
                settings,
            )
            new_status = "skipped" if ok else "error"
            order.status = new_status
            note_suffix = (
                f"SELL {sell_shares}@{sell_price:.4f} → {msg}"
            )
            order.note = (order.note or "") + " | " + note_suffix
            session.commit()

            bus.publish(OrderEvent(
                event_id="",
                order_id=order_id,
                leader_wallet="me",
                market_id=order.condition_id,
                asset_id=order.token_id,
                side="SELL",
                status=OrderStatus.FILLED if ok else OrderStatus.FAILED,
                reason=msg if not ok else None,
                data={
                    "source": "operator",
                    "follow_order_id": order.id,
                    "entry_price": entry_price,
                    "current_price": sell_price,
                    "size_shares": sell_shares,
                    "clob_message": msg,
                },
            ))
            return {
                "ok": ok,
                "follow_order_id": order.id,
                "status": new_status,
                "note": msg,
                "idempotent": False,
            }

    @app.post("/api/follow/manual-order")
    def follow_manual_order(payload: dict[str, Any]) -> dict[str, Any]:
        """Place (or dry-run) a single mirror order from the dashboard.

        Body schema::

            {
              "condition_id": "0xCOND...",
              "side": "BUY" | "SELL",
              "price": 0.42,
              "size_usdc": 50.0,
              "direction": "YES" | "NO",      # display only
              "asset_id": "0x...",            # optional, derived if missing
              "leader_wallet": "0x...",       # optional, defaults to "manual"
            }

        Returns one of ``dry_run`` / ``submitted`` / ``error`` /
        ``skipped`` plus the ``order_id`` for SSE correlation.
        """
        from .manual_order import (
            MAX_MANUAL_SIZE_USDC,
            MIN_MANUAL_SIZE_USDC,
            submit_manual_order,
        )

        condition_id = (payload.get("condition_id") or "").strip()
        if not condition_id:
            raise HTTPException(status_code=400, detail="condition_id required")

        try:
            side = str(payload.get("side") or "BUY").upper()
            price = float(payload.get("price"))
            size_usdc = float(payload.get("size_usdc"))
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail="side / price / size_usdc required and numeric",
            )

        if size_usdc <= 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"size_usdc must be > 0 (min {MIN_MANUAL_SIZE_USDC}, "
                    f"max {MAX_MANUAL_SIZE_USDC})"
                ),
            )

        with _session() as session:
            try:
                result = submit_manual_order(
                    session,
                    settings,
                    condition_id=condition_id,
                    side=side,
                    price=price,
                    size_usdc=size_usdc,
                    direction=str(payload.get("direction") or "YES").upper(),
                    asset_id=payload.get("asset_id"),
                    leader_wallet=payload.get("leader_wallet") or "manual",
                )
            except Exception as exc:
                session.rollback()
                logger.exception("manual order blew up")
                raise HTTPException(status_code=500, detail=f"manual order failed: {exc!r}")
            session.commit()
        return result

    @app.post("/api/follow/detect-sales")
    def follow_detect_sales(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """One-shot scan for follow-list SELL trades on markets we still hold.

        Body schema (all optional)::

            {
              "lookback_minutes": 30
            }

        The dashboard calls this on a slow interval (default 60s) so the
        *"leader already sold"* panel refreshes without polling the SSE
        stream alone.  The runtime loop also calls it on every tick.
        """
        from .manual_order import detect_leader_sales

        lookback = int((payload or {}).get("lookback_minutes") or 30)
        if lookback < 1 or lookback > 1440:
            raise HTTPException(status_code=400, detail="lookback_minutes 1..1440")

        with _session() as session:
            try:
                result = detect_leader_sales(
                    session, settings, lookback_minutes=lookback
                )
            except Exception as exc:
                session.rollback()
                logger.exception("detect_leader_sales blew up")
                raise HTTPException(status_code=500, detail=f"detect failed: {exc!r}")
            session.commit()
        return result

    # ------------------------------------------------------------------
    # Phase 2D: live copy-trade event stream — REMOVED 2026-07-26
    # ------------------------------------------------------------------
    # The "实时跟单事件" panel was removed from the dashboard; these
    # endpoints now return ``410 Gone`` so cached browser tabs (which
    # still hold the old polling JS) stop hammering the server with
    # /api/order-events/recent requests.  The routes are kept (rather
    # than deleted) so the deprecated URLs continue to answer cleanly
    # rather than 404 with a generic error.
    #
    # If the operator ever wants the panel back, the original
    # implementations live in git history under the ``order-events-
    # live-panel`` tag.

    @app.get("/api/order-events")
    def order_events_snapshot(
        limit: int = 100,
        live_only: bool = True,
        include_dry_run: bool | None = None,
    ) -> dict[str, Any]:
        """Deprecated — the live "实时跟单事件" panel was removed."""
        raise HTTPException(
            status_code=410,
            detail="order-events panel removed (2026-07-26); see git tag order-events-live-panel to restore",
        )

    @app.get("/api/order-events/recent")
    def order_events_recent(
        since_id: int = 0,
        limit: int = 50,
        live_only: bool = True,
        include_dry_run: bool | None = None,
        operator_only: bool = False,
    ) -> dict[str, Any]:
        """Deprecated — the live "实时跟单事件" panel was removed."""
        raise HTTPException(
            status_code=410,
            detail="order-events panel removed (2026-07-26); see git tag order-events-live-panel to restore",
        )

    @app.post("/api/order-events/trim")
    def order_events_trim(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Deprecated — kept for back-compat with any external cron."""
        raise HTTPException(
            status_code=410,
            detail="order-events panel removed (2026-07-26)",
        )

    @app.get("/api/order-events/stream")
    async def order_events_stream(request: __import__("fastapi").Request) -> Any:
        """Deprecated — the live "实时跟单事件" panel was removed."""
        raise HTTPException(
            status_code=410,
            detail="order-events panel removed (2026-07-26)",
        )

    @app.get("/api/routing-health")
    def routing_health() -> dict[str, Any]:
        """Phase 2: lane / circuit breaker / budget stats.

        Kept separate from /api/health (which only probes DB) so the
        dashboard's "status bar" can poll this cheaply every few
        seconds and so a DB outage does not mask the routing view.
        """
        from api import (
            get_limiter,
            get_breaker_registry,
            get_budget_registry,
        )
        return {
            "lanes": [s.__dict__ for s in get_limiter().stats()],
            "breakers": get_breaker_registry().stats(),
            "budgets": get_budget_registry().stats(),
        }

    # ------------------------------------------------------------------
    # End-to-end demo helper — publishes a few OrderEvents onto the
    # in-process bus so the SSE stream + UI annotations can be
    # exercised without a real signal hitting the executor.  Disabled
    # unless settings.live_trade OR an explicit env flag is set, to
    # avoid being callable in production by accident.
    # ------------------------------------------------------------------
    @app.post("/api/_demo/order-events")
    def demo_publish_order_events() -> dict[str, Any]:
        """Deprecated — the live "实时跟单事件" panel was removed."""
        raise HTTPException(
            status_code=410,
            detail="order-events panel removed (2026-07-26)",
        )

    @app.post("/api/collect/{job_name}")
    def trigger_collect(job_name: str, wallet_limit: int | None = None) -> dict[str, Any]:
        if job_name not in {"leaderboard", "markets", "trades", "positions", "all"}:
            raise HTTPException(status_code=400, detail="unknown job_name")
        with _session() as session:
            collector = SmartMoneyCollector(session, _client(settings), settings)
            if job_name == "leaderboard":
                collector.collect_leaderboard()
            elif job_name == "markets":
                wallets = collector._select_tracked_wallets(wallet_limit)
                collector.collect_market_metadata(wallets)
            elif job_name == "trades":
                wallets = collector._select_tracked_wallets(wallet_limit)
                collector.collect_activity(wallets)
            elif job_name == "positions":
                wallets = collector._select_tracked_wallets(wallet_limit)
                collector.collect_current_positions(wallets)
            else:
                collector.run_all(wallet_limit=wallet_limit)
            session.commit()
        return {"status": "ok", "job": job_name}

    app.mount("/static", StaticFiles(directory=str(dashboard_dir)), name="static")
    index = dashboard_dir / "index.html"

    @app.get("/")
    def root() -> FileResponse:
        if not index.exists():
            raise HTTPException(status_code=404, detail="dashboard/index.html missing")
        return FileResponse(str(index))

    return app


def _parse_wallets(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [w.strip().lower() for w in raw.split(",") if w.strip()]


def _fmt_wallet(wallet: str) -> str:
    if len(wallet) <= 10:
        return wallet
    return f"{wallet[:6]}...{wallet[-4:]}"


def _session():
    return get_session_factory()()


@dataclass
class _OrderPlanForApproval:
    """Subset of OrderPlan fields used by the approve endpoint."""
    token_id: str | None
    price: float
    size_usdc: float
    size_shares: float


def _build_plan_from_row(order, settings: SmartMoneySettings):
    """Build an OrderPlan from a stored FollowOrder row.

    Reuses the executor's internal dataclass so the live-submit path is
    identical to a fresh signal — same tick-rounding, same allowance
    checks, same `_submit_live` signature.
    """
    from .executor import OrderPlan
    return OrderPlan(
        signal_id=order.signal_id,
        wallet=order.wallet,
        condition_id=order.condition_id,
        direction=order.direction,
        token_id=order.token_id,
        side=order.side,
        price=order.price,
        size_usdc=order.size_usdc,
        size_shares=round(order.size_usdc / order.price, 2) if order.price else 0.0,
    )


def _client(settings: SmartMoneySettings) -> Any:
    from api import PolymarketPublicClient as PolymarketReadClient  # legacy name kept for compat
    return PolymarketReadClient(settings)


def _is_dry_run_event(ev) -> bool:
    """Return True if this ``OrderEvent`` was generated by the executor's
    DRY-RUN branch (not a real CLOB submission).

    Heuristic: the executor only ever publishes the literal string
    ``"dry-run simulated"`` (and only as the *reason* for a MIRRORED event)
    when no real order was sent.  We key off ``reason`` so any other
    MIRRORED signal — e.g. an on-chain confirmation — is correctly
    considered live.
    """
    if ev.status.value == "mirrored" and (ev.reason or "") == "dry-run simulated":
        return True
    if (ev.reason or "").startswith("dry-run"):
        return True
    return False


def _filter_events(events, *, live_only: bool):
    if not live_only:
        return list(events)
    return [ev for ev in events if not _is_dry_run_event(ev)]


def _event_to_dict(ev) -> dict[str, Any]:
    """Serialise an ``OrderEvent`` for JSON consumers (REST + SSE)."""
    base = {
        "event_id": ev.event_id,
        "order_id": ev.order_id,
        "leader_wallet": ev.leader_wallet,
        "market_id": ev.market_id,
        "asset_id": ev.asset_id,
        "side": ev.side,
        "status": ev.status.value,
        "reason": ev.reason,
        "data": ev.data,
        "ts": ev.ts,
        "live": not _is_dry_run_event(ev),
        "dry_run": _is_dry_run_event(ev),
        "attention": ev.status in (
            __import__("api").ATTENTION_STATUSES
        ),
    }
    return base


def _enrich_events_with_market(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Populate ``slug`` / ``event_slug`` / ``title`` on each event row.

    The bus only carries the bare condition_id.  When the dashboard
    renders a row we want a clickable link to the right Polymarket
    page, so we join against the local ``Market`` cache here.  Doing
    the join on the server keeps the SSR payloads small and avoids
    forcing every dashboard to issue its own /api/markets call.

    Fallback order:

    1. **Market cache** — populated by the collector when it sees a
       trade/position/leaderboard market.  This is the most accurate
       source (it has both the *event* and the *market* slug).
    2. **Signal table** — the consensus scanner stores ``title`` +
       ``slug`` per signal.  This catches orphan conditions where the
       Market cache has not yet been populated but a signal still
       fired.  Without this fallback the dashboard would render
       "搜索 0xa24cde…" links, which is meaningless to operators.
    """
    if not events:
        return events
    cond_ids = {ev.get("market_id") for ev in events if ev.get("market_id")}
    if not cond_ids:
        return events
    from .models import Market as _Market, Signal as _Signal

    slug_lookup: dict[str, dict[str, Any]] = {}
    signal_slug: dict[str, dict[str, Any]] = {}

    with _session() as session:
        # First pass: Market cache (most accurate).
        rows = (
            session.query(_Market)
            .filter(_Market.condition_id.in_(cond_ids))
            .all()
        )
        for r in rows:
            slug_lookup[r.condition_id] = {
                "slug": r.slug,
                "event_slug": getattr(r, "event_slug", None),
                "title": r.question,
            }
        # Second pass: Signal table fallback for orphan conds.
        missing = cond_ids - slug_lookup.keys()
        if missing:
            sig_rows = (
                session.query(_Signal.condition_id, _Signal.title, _Signal.slug)
                .filter(_Signal.condition_id.in_(missing))
                .order_by(_Signal.id.desc())
                .all()
            )
            # We pick the latest signal per cond — earlier rows are
            # older and may have stale slugs.
            for r in sig_rows:
                if r.condition_id in slug_lookup or r.condition_id in signal_slug:
                    continue
                signal_slug[r.condition_id] = {
                    "slug": r.slug,
                    "event_slug": None,
                    "title": r.title,
                }
    for ev in events:
        cond = ev.get("market_id") or ""
        info = slug_lookup.get(cond) or signal_slug.get(cond)
        if not info:
            continue
        ev.setdefault("slug", info.get("slug"))
        ev.setdefault("event_slug", info.get("event_slug"))
        ev.setdefault("title", info.get("title"))
    return events


def _enrich_events_with_price(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach ``current_price`` / ``delta_pct`` to each event for the table.

    For every event that carries a ``market_id`` we look up the latest
    ``CurrentPosition.current_price`` (the CLOB mark from the most
    recent positions tick).  If we also have an entry price
    (either from ``data.entry_price`` or by joining to the matching
    ``FollowOrder`` row), we surface the move as ``delta_pct``.

    We do NOT hit the CLOB live here — that would either rate-limit
    us (token bucket is throttled to 4 req/s) or 5xx.  The positions
    tick refreshes the mark every 5 min, which is fast enough for
    "实时跟单事件" UX.

    Order of preference for *current price*:
      1. ``CurrentPosition.current_price`` (most recent observation)
      2. fall back to the entry price (so a pending order shows its
         own limit rather than blank)

    Order of preference for *entry price*:
      1. ``data.entry_price`` (set by submit_manual_order /
         approve_pending_order; the executor's auto signals also
         include ``price`` in data)
      2. ``FollowOrder.price`` joined by ``data.follow_order_db_id``
      3. (none → leave delta_pct empty)
    """
    if not events:
        return events
    cond_ids = {ev.get("market_id") for ev in events if ev.get("market_id")}
    if not cond_ids:
        return events

    # Group events by (wallet, condition) so we can pick the matching
    # CurrentPosition (positions are per-wallet-per-condition).
    wallet_by_key: dict[tuple[str, str], str] = {}
    for ev in events:
        cond = ev.get("market_id") or ""
        wallet = ev.get("leader_wallet") or ""
        if cond and wallet and (wallet, cond) not in wallet_by_key:
            wallet_by_key[(wallet, cond)] = cond

    # Resolve entry prices from FollowOrder via data.follow_order_db_id
    # (operator-sourced events carry this; auto-signal events do not).
    db_ids = set()
    for ev in events:
        db_id = (ev.get("data") or {}).get("follow_order_db_id") or ev.get("follow_order_db_id")
        if db_id:
            db_ids.add(int(db_id))
    from .models import CurrentPosition, FollowOrder

    with _session() as session:
        cur_by_key: dict[tuple[str, str], float] = {}
        if wallet_by_key:
            wallets = {w for w, _ in wallet_by_key}
            conds = {c for _, c in wallet_by_key}
            for w, c, cp in session.query(
                CurrentPosition.wallet,
                CurrentPosition.condition_id,
                CurrentPosition.current_price,
            ).filter(
                CurrentPosition.wallet.in_(wallets),
                CurrentPosition.condition_id.in_(conds),
                CurrentPosition.size > 0,
            ).all():
                try:
                    cur_by_key[(w, c)] = float(cp or 0)
                except (TypeError, ValueError):
                    continue

        entry_by_db: dict[int, float] = {}
        if db_ids:
            for oid, price, side, condition_id, wallet in session.query(
                FollowOrder.id, FollowOrder.price, FollowOrder.side,
                FollowOrder.condition_id, FollowOrder.wallet,
            ).filter(FollowOrder.id.in_(db_ids)).all():
                entry_by_db[oid] = float(price or 0)

    for ev in events:
        cond = ev.get("market_id") or ""
        wallet = ev.get("leader_wallet") or ""
        data = ev.get("data") or {}
        entry = data.get("entry_price")
        if entry is None:
            db_id = data.get("follow_order_db_id") or ev.get("follow_order_db_id")
            if db_id:
                entry = entry_by_db.get(int(db_id))
        if entry is None:
            # Fall back to the event's own data.price (executor emits
            # {"price": …} for auto signals).
            entry = data.get("price")
        cur = cur_by_key.get((wallet, cond))
        try:
            entry_f = float(entry) if entry is not None else None
        except (TypeError, ValueError):
            entry_f = None
        try:
            cur_f = float(cur) if cur is not None else None
        except (TypeError, ValueError):
            cur_f = None
        if entry_f is not None:
            ev["entry_price"] = entry_f
        if cur_f is not None and cur_f > 0:
            ev["current_price"] = cur_f
        elif entry_f is not None:
            # No live mark yet (order hasn't filled or position tick
            # hasn't run) — surface the entry as the only price we
            # have so the operator can see *something*.
            ev["current_price"] = entry_f
        # delta_pct: (current - entry) / entry * 100
        if entry_f and cur_f is not None and entry_f > 0:
            ev["delta_pct"] = (cur_f - entry_f) / entry_f * 100
    return events


app = get_app()
