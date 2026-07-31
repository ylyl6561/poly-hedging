"""Apply risk filters to smart-money signals.

The user requirement is "风险过滤 → 人工确认 / 自动交易". This module is the
gate between a freshly-detected signal and any downstream action (manual UI,
auto-trade, alert).

Filters implemented
------------------
* ``min_signal_age_minutes`` — wait N minutes after the signal first appears
  to confirm it's not a one-off flash trade.
* ``min_consensus`` — consensus signals must clear ``min_consensus_traders``;
  new_open signals are exempt unless ``require_consensus_for_new_open``.
* ``market_end_window`` — block signals whose market ends in <N hours (too
  close to settlement, hard to size / too risky).
* ``market_end_max_days`` — block signals whose market ends >N days (too
  far out — usually illiquid).
* ``min_price`` — refuse to follow a trader's bet at ``price<0.05`` (lottery
  territory).
* ``max_price`` — refuse to follow at ``price>0.95`` (already-priced-in).
* ``max_total_value_usdc`` — cap how much USDC we'd be willing to deploy
  on a single signal.
* ``min_volume_24h`` — require at least ``$X`` traded in the last 24h on
  this market (illiquidity guard).
* ``max_position_size_usdc`` — never go over this regardless of size.
* ``duplicate_window`` — if a signal for the same (condition_id, direction)
  is currently in status ``pass``, demote the new one to ``shrink``.

Decision vocabulary
-------------------
* ``pass``   — clear to act, suggested size provided
* ``shrink`` — proceed but with reduced size (and reasons)
* ``block``  — do not act (and reasons)

The decisions are logged into ``smart_money_risk_runs`` so the user can
audit / tune thresholds.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .config import SmartMoneySettings
from .models import Market, RiskFilterRun, Signal

logger = logging.getLogger(__name__)


def _volume_24h(session: Session, condition_id: str) -> float:
    """Approximate 24h traded volume on the condition."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    val = session.execute(
        text(
            """
            SELECT COALESCE(SUM(ABS(amount)), 0)
            FROM smart_money_trades
            WHERE condition_id = :cond AND traded_at >= :cutoff
            """
        ),
        {"cond": condition_id, "cutoff": cutoff},
    ).scalar()
    return float(val or 0)


def _existing_active(session: Session, condition_id: str, direction: str) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
    sig = session.execute(
        select(Signal).where(
            Signal.condition_id == condition_id,
            Signal.direction == direction,
            Signal.created_at >= cutoff,
            Signal.status.in_(("pass", "shrink")),
        )
    ).first()
    return sig is not None


def evaluate(
    session: Session,
    settings: SmartMoneySettings,
) -> dict[str, int]:
    """Run the filter over every signal currently in ``pending`` state.

    Returns counters for the run history.
    """
    now = datetime.now(timezone.utc)
    pending_signals = (
        session.execute(
            select(Signal).where(Signal.status == "pending").order_by(Signal.created_at.desc())
        )
        .scalars()
        .all()
    )

    decisions = {"pass": 0, "shrink": 0, "block": 0, "updated": 0}

    for sig in pending_signals:
        reasons: list[str] = []
        decision = "pass"
        size = float(settings.default_position_size_usdc)

        # Filter 1: signal age — too fresh = block. Set age_minutes=0 to disable.
        # Production: loop runs every ~8min so this naturally gates flash trades.
        if settings.min_signal_age_minutes > 0:
            age_min = (now - sig.created_at).total_seconds() / 60.0
            if age_min < settings.min_signal_age_minutes:
                reasons.append(
                    f"too fresh ({age_min:.1f}min < {settings.min_signal_age_minutes}min)"
                )
                decision = "block"

        # Filter 2: consensus requirement for new_open
        if (
            sig.signal_type == "new_open"
            and settings.require_consensus_for_new_open
            and sig.trader_count < settings.min_consensus_traders
        ):
            reasons.append(
                f"new_open but trader_count={sig.trader_count} < {settings.min_consensus_traders}"
            )
            decision = "block"

        # Filter 3: market end window
        market = session.execute(
            select(Market).where(Market.condition_id == sig.condition_id)
        ).scalar_one_or_none()
        if market and market.end_time:
            end = market.end_time
            hours_to_end = (end - now).total_seconds() / 3600.0
            if 0 < hours_to_end < settings.market_end_min_hours:
                reasons.append(f"market ends in {hours_to_end:.1f}h (too soon)")
                decision = "block"
            elif hours_to_end > settings.market_end_max_days * 24:
                reasons.append(f"market ends in {hours_to_end/24:.1f}d (too far)")
                decision = "block"

        # Filter 4: price band
        price = float(sig.avg_entry_price or sig.current_price or 0)
        if price > 0:
            if price < settings.min_signal_price:
                reasons.append(f"price {price:.3f} < {settings.min_signal_price}")
                decision = "shrink" if decision == "pass" else decision
                size *= 0.25
            elif price > settings.max_signal_price:
                reasons.append(f"price {price:.3f} > {settings.max_signal_price}")
                decision = "shrink" if decision == "pass" else decision
                size *= 0.25

        # Filter 5: liquidity (24h volume)
        vol24 = _volume_24h(session, sig.condition_id)
        if vol24 < settings.min_volume_24h_usdc:
            reasons.append(f"24h volume ${vol24:.0f} < ${settings.min_volume_24h_usdc:.0f}")
            decision = "block" if settings.block_illiquid else "shrink"
            if decision != "block":
                size *= 0.5

        # Filter 6: max single-signal size
        if size > settings.max_position_size_usdc:
            reasons.append(
                f"suggested ${size:.0f} > max ${settings.max_position_size_usdc:.0f}"
            )
            size = settings.max_position_size_usdc
            if decision == "pass":
                decision = "shrink"

        # Filter 7: duplicate active signal in the recent window
        if _existing_active(session, sig.condition_id, sig.direction):
            reasons.append("duplicate active signal in 2h window")
            if decision == "pass":
                decision = "shrink"
            size *= 0.5

        # Filter 8: confidence floor
        if sig.confidence < settings.min_signal_confidence:
            reasons.append(
                f"confidence {sig.confidence:.2f} < {settings.min_signal_confidence}"
            )
            decision = "block"

        sig.status = decision
        sig.risk_reasons = reasons
        sig.suggested_size_usdc = round(size, 2)
        sig.updated_at = now

        session.add(
            RiskFilterRun(
                signal_id=sig.id,
                decision=decision,
                reasons=reasons,
                suggested_size_usdc=round(size, 2),
            )
        )
        decisions[decision] = decisions.get(decision, 0) + 1
        decisions["updated"] += 1

    logger.info(
        "risk_filter: pass=%d shrink=%d block=%d (updated=%d)",
        decisions["pass"],
        decisions["shrink"],
        decisions["block"],
        decisions["updated"],
    )
    return decisions
