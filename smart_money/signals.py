"""Detect Smart Money signals from trades + positions.

Two signal flavours:
* ``new_open`` — a tracked trader enters a condition_id we haven't seen them
  in within the recent window (likely new conviction bet).
* ``consensus`` — N+ tracked traders open the *same* condition with the same
  outcome in the same window (group conviction).

Each signal is written to ``smart_money_signals`` and routed through the
risk filter so the user can act on a ``status='pass'`` row immediately.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, select, text
from sqlalchemy.orm import Session

from .config import SmartMoneySettings
from .models import (
    CurrentPosition,
    Market,
    Signal,
    Trader,
    TraderScore,
    Trade,
)

logger = logging.getLogger(__name__)


def _new_open_signals(
    session: Session,
    *,
    recent_hours: int,
    min_trader_score: float,
) -> list[dict[str, Any]]:
    """Detect first-time-in-N-hours entries by tracked high-score traders.

    Uses a per-wallet "last time we saw them trade this condition" check.
    New trades that don't match any prior trade inside the window are
    flagged as a new-open signal.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=recent_hours)
    history_cutoff = cutoff - timedelta(days=90)  # look at last 90d for prior presence

    # Pull recent trades by tracked traders with score >= threshold
    base_q = (
        select(Trade, TraderScore.smart_money_score, Trader.username, Trader.pseudonym)
        .join(Trader, Trader.wallet == Trade.wallet)
        .outerjoin(
            TraderScore,
            TraderScore.wallet == Trade.wallet,
        )
        .where(
            Trade.traded_at >= cutoff,
            Trade.side == "BUY",
            Trader.tracked.is_(True),
            (TraderScore.smart_money_score >= min_trader_score) | (TraderScore.smart_money_score.is_(None)),
        )
        .order_by(Trade.traded_at.desc())
        .limit(500)
    )
    rows = session.execute(base_q).all()

    if not rows:
        return []

    # Group by (wallet, condition_id); keep the earliest trade in this window
    grouped: dict[tuple[str, str], list] = {}
    for trade, score, username, pseudonym in rows:
        key = (trade.wallet, trade.condition_id)
        grouped.setdefault(key, []).append((trade, score, username, pseudonym))

    # For each key, check if there were earlier trades inside history_cutoff..cutoff
    new_open_keys: list[tuple[str, str]] = []
    for (wallet, cond), entries in grouped.items():
        prior = session.execute(
            select(func.count(Trade.fingerprint)).where(
                Trade.wallet == wallet,
                Trade.condition_id == cond,
                Trade.traded_at >= history_cutoff,
                Trade.traded_at < cutoff,
            )
        ).scalar() or 0
        if prior == 0:
            new_open_keys.append((wallet, cond))

    if not new_open_keys:
        return []

    # Resolve market metadata for the conditions
    cond_ids = list({c for _, c in new_open_keys})
    market_rows = {
        m.condition_id: m
        for m in session.execute(
            select(Market).where(Market.condition_id.in_(cond_ids))
        ).scalars().all()
    }

    signals: list[dict[str, Any]] = []
    for (wallet, cond), entries in grouped.items():
        if (wallet, cond) not in new_open_keys:
            continue
        # Take the largest trade in this window as the "anchor"
        anchor = max(entries, key=lambda e: float(e[0].amount or 0))
        trade, score, username, pseudonym = anchor
        market = market_rows.get(cond)
        outcome_index = trade.outcome_index
        direction = "YES" if outcome_index in (None, 0) else "NO"

        signals.append(
            {
                "signal_type": "new_open",
                "condition_id": cond,
                "outcome_index": outcome_index,
                "outcome_label": trade.outcome or direction,
                "direction": direction,
                "trigger_wallets": [
                    {
                        "wallet": wallet,
                        "username": username,
                        "pseudonym": pseudonym,
                        "smart_money_score": float(score or 0),
                        "amount": float(trade.amount or 0),
                        "price": float(trade.price or 0),
                        "traded_at": trade.traded_at.isoformat() if trade.traded_at else None,
                        "trade_id": trade.fingerprint,
                    }
                ],
                "trader_count": 1,
                "trigger_trade_fingerprint": trade.fingerprint,
                "title": market.question if market else trade.title,
                "slug": market.slug if market else trade.slug,
                "category": market.category if market else None,
                "end_time": market.end_time if market else None,
                "total_value": float(trade.amount or 0),
                "avg_entry_price": float(trade.price or 0),
                "current_price": float(trade.price or 0),
                "confidence": min(0.99, 0.5 + (float(score or 0) / 200.0)),
                "status": "pending",
                "risk_reasons": [],
                "suggested_size_usdc": 0.0,
            }
        )
    return signals


def _consensus_signals(
    session: Session,
    *,
    recent_hours: int,
    min_traders: int,
    min_trader_score: float,
) -> list[dict[str, Any]]:
    """Group all tracked high-score recent BUYs by (condition_id, outcome_index)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=recent_hours)

    rows = session.execute(
        select(Trade, TraderScore.smart_money_score, Trader.username, Trader.pseudonym, Market)
        .join(Trader, Trader.wallet == Trade.wallet)
        .outerjoin(TraderScore, TraderScore.wallet == Trade.wallet)
        .outerjoin(Market, Market.condition_id == Trade.condition_id)
        .where(
            Trade.traded_at >= cutoff,
            Trade.side == "BUY",
            Trader.tracked.is_(True),
            (TraderScore.smart_money_score >= min_trader_score) | (TraderScore.smart_money_score.is_(None)),
        )
        .order_by(Trade.traded_at.desc())
        .limit(2000)
    ).all()

    if not rows:
        return []

    groups: dict[tuple[str, int | None], list] = {}
    for trade, score, username, pseudonym, market in rows:
        key = (trade.condition_id, trade.outcome_index)
        groups.setdefault(key, []).append((trade, score, username, pseudonym, market))

    signals: list[dict[str, Any]] = []
    for (cond, outcome_index), entries in groups.items():
        wallets = list({e[0].wallet for e in entries})
        if len(wallets) < min_traders:
            continue
        first = entries[0]
        _, _, _, _, market = first
        direction = "YES" if outcome_index in (None, 0) else "NO"
        total_value = sum(float(t.amount or 0) for t, *_ in entries)
        prices = [float(t.price or 0) for t, *_ in entries if t.price is not None]
        avg_entry = sum(prices) / len(prices) if prices else 0.0
        max_score = max((float(score or 0) for _, score, _, _, _ in entries), default=0.0)
        signals.append(
            {
                "signal_type": "consensus",
                "condition_id": cond,
                "outcome_index": outcome_index,
                "outcome_label": entries[0][0].outcome or direction,
                "direction": direction,
                "trigger_wallets": [
                    {
                        "wallet": e[0].wallet,
                        "username": e[2],
                        "pseudonym": e[3],
                        "smart_money_score": float(e[1] or 0),
                        "amount": float(e[0].amount or 0),
                        "price": float(e[0].price or 0),
                        "traded_at": e[0].traded_at.isoformat() if e[0].traded_at else None,
                        "trade_id": e[0].fingerprint,
                    }
                    for e in entries
                ],
                "trader_count": len(wallets),
                # pick the largest trade as the "anchor" — useful for the executor
                "trigger_trade_fingerprint": max(
                    entries, key=lambda e: float(e[0].amount or 0)
                )[0].fingerprint,
                "title": market.question if market else entries[0][0].title,
                "slug": market.slug if market else entries[0][0].slug,
                "category": market.category if market else None,
                "end_time": market.end_time if market else None,
                "total_value": round(total_value, 2),
                "avg_entry_price": round(avg_entry, 4),
                "current_price": round(avg_entry, 4),
                "confidence": min(0.99, 0.4 + len(wallets) * 0.05 + min(0.3, max_score / 200)),
                "status": "pending",
                "risk_reasons": [],
                "suggested_size_usdc": 0.0,
            }
        )
    return signals


def detect_signals(
    session: Session,
    settings: SmartMoneySettings,
) -> dict[str, int]:
    """Run new_open + consensus detection and persist the results."""
    new_opens = _new_open_signals(
        session,
        recent_hours=settings.signal_recent_hours,
        min_trader_score=settings.signal_min_trader_score,
    )
    consensus = _consensus_signals(
        session,
        recent_hours=settings.signal_recent_hours,
        min_traders=settings.min_consensus_traders,
        min_trader_score=settings.signal_min_trader_score,
    )

    all_signals = new_opens + consensus
    if not all_signals:
        logger.info("detect_signals: no signals")
        return {"new_open": 0, "consensus": 0, "written": 0}

    # Dedupe: per (signal_type, condition_id, direction) keep the most recent
    # (latest trade_at inside trigger_wallets).
    deduped: dict[tuple, dict] = {}
    for s in all_signals:
        key = (s["signal_type"], s["condition_id"], s["direction"])
        if key not in deduped:
            deduped[key] = s
            continue
        cur = deduped[key]
        cur_ts = max(
            (w.get("traded_at") or "") for w in cur["trigger_wallets"]
        )
        new_ts = max(
            (w.get("traded_at") or "") for w in s["trigger_wallets"]
        )
        if new_ts > cur_ts:
            deduped[key] = s
        else:
            # merge wallets lists to keep all triggering wallets
            seen = {w["wallet"] for w in cur["trigger_wallets"]}
            for w in s["trigger_wallets"]:
                if w["wallet"] not in seen:
                    cur["trigger_wallets"].append(w)
                    seen.add(w["wallet"])
            cur["trader_count"] = len(seen)

    # Dedupe against existing signals for the same key in the last hour
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    rows = session.execute(
        select(Signal).where(Signal.created_at >= one_hour_ago)
    ).scalars().all()
    existing_keys = {(r.signal_type, r.condition_id, r.direction): r for r in rows}

    written = 0
    for (sig_type, cond, direction), payload in deduped.items():
        if (sig_type, cond, direction) in existing_keys:
            existing = existing_keys[(sig_type, cond, direction)]
            existing.trigger_wallets = payload["trigger_wallets"]
            existing.trader_count = payload["trader_count"]
            existing.total_value = payload["total_value"]
            existing.avg_entry_price = payload["avg_entry_price"]
            existing.current_price = payload["current_price"]
            existing.confidence = payload["confidence"]
            existing.updated_at = datetime.now(timezone.utc)
        else:
            session.add(Signal(**payload))
        written += 1

    logger.info(
        "detect_signals: new_open=%d consensus=%d written_or_updated=%d",
        len(new_opens),
        len(consensus),
        written,
    )
    return {
        "new_open": len(new_opens),
        "consensus": len(consensus),
        "written": written,
    }
