"""Generate the copy-trading follow list from short-window trader
scores + a user-curated ``ManualFollow`` override table.

Where :mod:`smart_money.window_scoring` produces the raw window
metrics (last 30 days PnL/ROI/days_active/open_pos/profit_days/
loss_days/max_drawdown/...), this module selects the *followable*
subset using thresholds configured in ``SmartMoneySettings`` and
layers in the user-curated :class:`ManualFollow` table.

Why this exists
---------------
The user wants the follow list to be wallets that are:

1. **Consistently profitable** — every active day inside the 30-day
   window must net positive realised PnL (``loss_days == 0``).
2. **Stable** — low drawdown (``max_drawdown_pct <= ~30%``).
3. **Active right now** — currently holding positions AND traded
   within ``follow_max_idle_days``.
4. **Not a high-frequency day-trader** — ``trade_count <= 30`` inside
   the window.
5. **Has a real history** — ``period_days >= 14`` (kills single-week
   bursts that print 200% in 5 days then disappear).

The list is then merged with :class:`ManualFollow`.  Manual entries
bypass the PnL / loss_days / drawdown gates but still must be active
(we won't follow a dead wallet).  This lets the operator pin a wallet
they've hand-vetted.

The :class:`FollowListEntry` table is the *materialised* result; the
follow-loop / executor reads it directly without re-running the
filter.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import SmartMoneySettings
from .models import (
    CurrentPosition,
    FollowListEntry,
    ManualFollow,
    Trade,
    Trader,
    WindowScore,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Snapshot persistence — survives process boundaries so the dashboard API
# (which lives in a separate uvicorn worker) can read what the loop wrote.
# --------------------------------------------------------------------------

_SNAPSHOT_PATH = Path(tempfile.gettempdir()) / "smart_money_follow_last_run.json"


def _write_follow_last_run(snapshot: dict[str, Any]) -> None:
    """Atomically write the follow-list rejection snapshot."""
    try:
        tmp = _SNAPSHOT_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot, default=str))
        os.replace(tmp, _SNAPSHOT_PATH)
    except Exception as exc:  # noqa: BLE001
        logger.warning("write follow_last_run snapshot failed: %s", exc)


def _read_follow_last_run() -> dict[str, Any] | None:
    if not _SNAPSHOT_PATH.exists():
        return None
    try:
        return json.loads(_SNAPSHOT_PATH.read_text())
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# Selection criteria
# --------------------------------------------------------------------------


def _filter_candidate(
    c: Any,
    settings: SmartMoneySettings,
    idle_cutoff_days: int,
) -> tuple[bool, list[str]]:
    """Run the *auto* follow-list filter against a single WindowScore row.

    Returns ``(passed, reasons)``.  ``passed=True`` means this wallet
    qualifies under the user-defined stability / recency / frequency
    rules.  ``reasons`` lists every gate that *failed* (used for the
    dashboard's "rejected candidates" panel).
    """
    reasons: list[str] = []

    # Phase 5: stability gates (the new ones).
    if c.loss_days > settings.follow_max_loss_days:
        reasons.append(f"loss_days>{settings.follow_max_loss_days}")
    if c.max_drawdown_pct is not None and c.max_drawdown_pct > settings.follow_max_drawdown_pct:
        reasons.append(f"drawdown>{settings.follow_max_drawdown_pct:.0f}%")
    if c.trade_count > settings.follow_max_trade_count:
        reasons.append(f"trade_count>{settings.follow_max_trade_count}")
    period = float(getattr(c, "period_days", 0.0) or 0.0)
    if period < settings.follow_min_period_days:
        reasons.append(f"period<{settings.follow_min_period_days:.0f}d")

    # Original gates.
    if c.smart_window_score < settings.follow_min_window_score:
        reasons.append(f"score<{settings.follow_min_window_score}")
    if c.days_active < settings.follow_min_days_active:
        reasons.append(f"days_active<{settings.follow_min_days_active}")
    if c.open_position_count < settings.follow_min_open_positions:
        reasons.append(f"open_pos<{settings.follow_min_open_positions}")
    if c.realized_pnl < settings.follow_min_window_pnl:
        reasons.append(f"window_pnl<{settings.follow_min_window_pnl}")
    if c.days_since_active is not None and c.days_since_active > idle_cutoff_days:
        reasons.append(f"idle>{idle_cutoff_days}d")
    elif c.days_since_active is None:
        reasons.append(f"idle>{idle_cutoff_days}d")
    if c.trade_count < settings.follow_min_trade_count:
        reasons.append(f"trade_count<{settings.follow_min_trade_count}")

    return (not reasons, reasons)


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------


def refresh_follow_list(session: Session, settings: SmartMoneySettings) -> list[str]:
    """Recompute the follow list and return the top-N wallets.

    Selection criteria — all must hold for ``auto`` entries:

    * ``loss_days``           <= ``follow_max_loss_days`` (default 0)
    * ``max_drawdown_pct``    <= ``follow_max_drawdown_pct`` (default 30)
    * ``trade_count``         <= ``follow_max_trade_count`` (default 30)
    * ``period_days``         >= ``follow_min_period_days`` (default 14)
    * ``smart_window_score``  >= ``follow_min_window_score`` (default 40)
    * ``days_active``         >= ``follow_min_days_active`` (default 5)
    * ``open_position_count`` >= ``follow_min_open_positions`` (default 1)
    * ``realized_pnl``        >= ``follow_min_window_pnl`` (default $100)
    * ``days_since_active``   <= ``follow_max_idle_days`` (default 3)
    * ``trade_count``         >= ``follow_min_trade_count`` (default 3)

    Manual entries (from :class:`ManualFollow`) bypass everything
    *except* the recency check (we won't follow a dead wallet).  The
    operator is responsible for verifying PnL / drawdown / win-streak
    for any manual entry.
    """
    now = datetime.now(timezone.utc)
    idle_cutoff_days = settings.follow_max_idle_days

    # ----- (1) Pull every 30-day WindowScore row + manual list -----
    candidates_q = (
        select(
            WindowScore.wallet,
            WindowScore.username,
            WindowScore.pseudonym,
            WindowScore.verified,
            WindowScore.smart_window_score,
            WindowScore.win_rate,
            WindowScore.roi_pct,
            WindowScore.realized_pnl,
            WindowScore.total_pnl,
            WindowScore.unrealized_pnl,
            WindowScore.trade_count,
            WindowScore.closed_count,
            WindowScore.days_active,
            WindowScore.open_position_count,
            WindowScore.last_active_at,
            WindowScore.days_since_active,
            WindowScore.top_category,
            WindowScore.loss_days,
            WindowScore.max_drawdown_pct,
            WindowScore.period_days,
            WindowScore.profit_days,
            WindowScore.longest_win_streak,
            WindowScore.daily_pnl_stddev,
        )
        .where(WindowScore.window_days == 30)
        .order_by(WindowScore.smart_window_score.desc())
    )
    candidates = session.execute(candidates_q).all()
    by_wallet = {c.wallet: c for c in candidates}

    manual_rows = session.execute(select(ManualFollow)).scalars().all()
    manual_wallets = {m.wallet: m for m in manual_rows}

    # ----- (2) Apply the auto filter + record rejections -----
    selected: list[dict[str, Any]] = []
    rejected: dict[str, int] = {}
    rejection_breakdown: list[dict[str, Any]] = []
    for c in candidates:
        passed, reasons = _filter_candidate(c, settings, idle_cutoff_days)
        if not passed:
            for r in reasons:
                rejected[r] = rejected.get(r, 0) + 1
            rejection_breakdown.append({
                "wallet": c.wallet,
                "username": c.username or c.pseudonym or c.wallet[:10],
                "smart_window_score": c.smart_window_score,
                "roi_pct": c.roi_pct,
                "realized_pnl": c.realized_pnl,
                "days_active": c.days_active,
                "open_position_count": c.open_position_count,
                "trade_count": c.trade_count,
                "closed_count": c.closed_count,
                "days_since_active": c.days_since_active,
                "loss_days": c.loss_days,
                "max_drawdown_pct": c.max_drawdown_pct,
                "period_days": getattr(c, "period_days", 0.0),
                "profit_days": getattr(c, "profit_days", 0),
                "top_category": c.top_category,
                "reasons": reasons,
                "primary_reason": reasons[0],
                "source": "auto",
            })
            continue

        selected.append({
            "wallet": c.wallet,
            "username": c.username,
            "pseudonym": c.pseudonym,
            "verified": bool(c.verified),
            "smart_money_score": float(c.smart_window_score or 0.0),
            "win_rate": float(c.win_rate or 0.0),
            "roi_pct": float(c.roi_pct or 0.0),
            "realized_pnl": float(c.realized_pnl or 0.0),
            "total_pnl": float(c.total_pnl or 0.0),
            "days_active": int(c.days_active or 0),
            "open_position_count": int(c.open_position_count or 0),
            "trade_count": int(c.trade_count or 0),
            "closed_count": int(c.closed_count or 0),
            "last_active_at": c.last_active_at,
            "days_since_active": float(c.days_since_active or 0.0),
            "top_category": c.top_category,
            "loss_days": int(getattr(c, "loss_days", 0) or 0),
            "max_drawdown_pct": float(getattr(c, "max_drawdown_pct", 0.0) or 0.0),
            "period_days": float(getattr(c, "period_days", 0.0) or 0.0),
            "source": "auto",
            "note": None,
        })

    # ----- (3) Layer in ManualFollow -----
    # Manual entries: get the WalletScore row if present (so we can
    # show ROI / loss_days etc. on the dashboard) but bypass all the
    # *quality* gates.  We *do* still require the wallet to have
    # traded within ``follow_max_idle_days`` — there's no point
    # following a wallet that's been silent for a week.
    manual_rejection_breakdown: list[dict[str, Any]] = []
    for m in manual_rows:
        c = by_wallet.get(m.wallet)
        # Always-on recency gate (every entry, auto or manual).
        days_since = (
            float(c.days_since_active) if c is not None and c.days_since_active is not None
            else 999.0
        )
        if days_since > idle_cutoff_days:
            manual_rejection_breakdown.append({
                "wallet": m.wallet,
                "username": (c.username if c else None) or m.username or m.wallet[:10],
                "smart_window_score": float(c.smart_window_score) if c else 0.0,
                "roi_pct": float(c.roi_pct) if c else 0.0,
                "realized_pnl": float(c.realized_pnl) if c else 0.0,
                "days_active": int(c.days_active) if c else 0,
                "open_position_count": int(c.open_position_count) if c else 0,
                "trade_count": int(c.trade_count) if c else 0,
                "closed_count": int(c.closed_count) if c else 0,
                "days_since_active": days_since,
                "loss_days": int(c.loss_days) if c else None,
                "max_drawdown_pct": float(c.max_drawdown_pct) if c else None,
                "period_days": float(getattr(c, "period_days", 0.0)) if c else 0.0,
                "profit_days": int(getattr(c, "profit_days", 0)) if c else 0,
                "top_category": c.top_category if c else None,
                "reasons": [f"idle>{idle_cutoff_days}d"],
                "primary_reason": f"idle>{idle_cutoff_days}d",
                "source": "manual",
            })
            continue

        # Bypass all quality gates.  Build the row.
        selected.append({
            "wallet": m.wallet,
            "username": (c.username if c else None) or m.username,
            "pseudonym": c.pseudonym if c else None,
            "verified": bool(c.verified) if c else False,
            "smart_money_score": float(c.smart_window_score or 0.0) if c else 0.0,
            "win_rate": float(c.win_rate or 0.0) if c else 0.0,
            "roi_pct": float(c.roi_pct or 0.0) if c else 0.0,
            "realized_pnl": float(c.realized_pnl or 0.0) if c else 0.0,
            "total_pnl": float(c.total_pnl or 0.0) if c else 0.0,
            "days_active": int(c.days_active or 0) if c else 0,
            "open_position_count": int(c.open_position_count or 0) if c else 0,
            "trade_count": int(c.trade_count or 0) if c else 0,
            "closed_count": int(c.closed_count or 0) if c else 0,
            "last_active_at": c.last_active_at if c else None,
            "days_since_active": days_since,
            "top_category": c.top_category if c else None,
            "loss_days": int(c.loss_days or 0) if c else 0,
            "max_drawdown_pct": float(c.max_drawdown_pct or 0.0) if c else 0.0,
            "period_days": float(getattr(c, "period_days", 0.0) or 0.0) if c else 0.0,
            "source": "manual",
            "note": m.note,
        })

    # ----- (4) Rank: manual entries first (operator-pinned), then auto
    #           by window_score.  Then dedupe by wallet (manual wins). -----
    manual_selected = [r for r in selected if r["source"] == "manual"]
    auto_selected = [r for r in selected if r["source"] != "manual"]
    auto_selected.sort(key=lambda r: r["smart_money_score"], reverse=True)

    # Manual rank starts at 1; auto continues from len(manual_selected)+1.
    rows: list[dict[str, Any]] = []
    rank = 1
    seen: set[str] = set()
    for r in manual_selected:
        if r["wallet"] in seen:
            continue
        seen.add(r["wallet"])
        r["rank"] = rank
        rows.append(r)
        rank += 1
    for r in auto_selected:
        if r["wallet"] in seen:
            continue
        seen.add(r["wallet"])
        r["rank"] = rank
        rows.append(r)
        rank += 1

    # ----- (5) Wipe + insert the persisted follow list. -----
    session.execute(FollowListEntry.__table__.delete())
    if rows:
        session.bulk_save_objects([
            FollowListEntry(
                wallet=r["wallet"],
                rank=r["rank"],
                username=r["username"],
                pseudonym=r["pseudonym"],
                verified=r["verified"],
                smart_money_score=r["smart_money_score"],
                win_rate=r["win_rate"],
                roi_pct=r["roi_pct"],
                realized_pnl=r["realized_pnl"],
                closed_count=r["closed_count"],
                top_category=r["top_category"],
                source=r["source"],
                note=r["note"],
            )
            for r in rows
        ])

    # ----- (6) Top-N wallet list for the follow executor. -----
    top_wallets = [r["wallet"] for r in rows[: settings.follow_top_n_for_signals]]

    auto_count = sum(1 for r in rows if r["source"] == "auto")
    manual_count = sum(1 for r in rows if r["source"] == "manual")
    logger.info(
        "refresh_follow_list: candidates=%d auto=%d manual=%d rejected=%d "
        "(thresholds: loss_days<=%d dd<=%.0f%% trades<=%d period>=%.0fd "
        "score>=%.1f days_active>=%d open_pos>=%d window_pnl>=%.0f "
        "idle<=%dd trade_count>=%d)",
        len(candidates),
        auto_count,
        manual_count,
        len(rejection_breakdown) + len(manual_rejection_breakdown),
        settings.follow_max_loss_days,
        settings.follow_max_drawdown_pct,
        settings.follow_max_trade_count,
        settings.follow_min_period_days,
        settings.follow_min_window_score,
        settings.follow_min_days_active,
        settings.follow_min_open_positions,
        settings.follow_min_window_pnl,
        settings.follow_max_idle_days,
        settings.follow_min_trade_count,
    )

    snapshot = {
        "generated_at": now.isoformat(),
        "thresholds": {
            "window_score": settings.follow_min_window_score,
            "days_active": settings.follow_min_days_active,
            "open_positions": settings.follow_min_open_positions,
            "window_pnl": settings.follow_min_window_pnl,
            "max_idle_days": settings.follow_max_idle_days,
            "trade_count_min": settings.follow_min_trade_count,
            "trade_count_max": settings.follow_max_trade_count,
            "loss_days_max": settings.follow_max_loss_days,
            "drawdown_max_pct": settings.follow_max_drawdown_pct,
            "period_min_days": settings.follow_min_period_days,
        },
        "totals": {
            "candidates": len(candidates),
            "auto_selected": auto_count,
            "manual_selected": manual_count,
            "rejected": len(rejection_breakdown) + len(manual_rejection_breakdown),
            "rejected_by_reason": rejected,
        },
        "rejected": sorted(
            rejection_breakdown + manual_rejection_breakdown,
            key=lambda r: (
                -r["smart_window_score"],
                r["username"] or "",
            ),
        ),
    }
    session.info["follow_last_run"] = snapshot
    _write_follow_last_run(snapshot)
    return top_wallets


# --------------------------------------------------------------------------
# Read-back (cheap)
# --------------------------------------------------------------------------


def get_follow_list(session: Session, *, top_n: int | None = None) -> list[dict[str, Any]]:
    """Read the persisted follow list (cheap).

    Annotates each row with the *current* open-position count and
    surface the ``source`` / ``note`` fields added in Phase 5.
    """
    stmt = select(FollowListEntry).order_by(FollowListEntry.rank)
    if top_n:
        stmt = stmt.limit(top_n)
    rows = session.execute(stmt).scalars().all()

    wallets = [r.wallet for r in rows]
    opens: dict[str, int] = {}
    if wallets:
        q = session.execute(
            select(CurrentPosition.wallet, func.count(CurrentPosition.token_id))
            .where(
                CurrentPosition.wallet.in_(wallets),
                CurrentPosition.size > 0,
            )
            .group_by(CurrentPosition.wallet)
        ).all()
        opens = {w: int(c) for w, c in q}

    return [
        {
            "rank": r.rank,
            "wallet": r.wallet,
            "wallet_full": r.wallet,
            "username": r.username or r.pseudonym or "—",
            "verified": r.verified,
            "smart_money_score": r.smart_money_score,
            "win_rate": r.win_rate,
            "roi_pct": r.roi_pct,
            "realized_pnl": r.realized_pnl,
            "closed_count": r.closed_count,
            "top_category": r.top_category,
            "source": getattr(r, "source", "auto") or "auto",
            "note": getattr(r, "note", None),
            "added_at": r.added_at.isoformat() if r.added_at else None,
            "open_position_count": opens.get(r.wallet, 0),
        }
        for r in rows
    ]