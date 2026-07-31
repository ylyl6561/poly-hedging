"""CLI for the Polymarket Smart Money Tracker (Phase 1, read-only).

Examples
--------
Initialize the schema:
    python -m smart_money init-db

Run the full collection pipeline once:
    python -m smart_money run --job all

Run only one job:
    python -m smart_money run --job trades
    python -m smart_money run --job positions --wallet-limit 200

Run the full pipeline forever in a single process (no extra scheduler needed).
The collector sleeps between cycles and runs each job on its own schedule:
    python -m smart_money run --job all --loop
    python -m smart_money run --job all --loop --loop-interval-seconds 300
    python -m smart_money run --job all --loop \
        --loop-trades-seconds 300 \
        --loop-positions-seconds 300 \
        --loop-markets-seconds 21600 \
        --loop-leaderboard-seconds 86400

Start the dashboard API server:
    python -m smart_money serve --host 0.0.0.0 --port 8088

Print a one-shot dashboard snapshot to stdout:
    python -m smart_money snapshot
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .analytics import SmartMoneyAnalytics
from api import PolymarketPublicClient as PolymarketReadClient  # legacy name kept for compat
from .collector import SmartMoneyCollector
from .config import SmartMoneySettings, get_settings
from .db import create_schema, get_engine, get_session_factory, session_scope
from .models import CollectionRun
from .normalization import utc_now


def _bootstrap_dotenv() -> None:
    """Load ``.env`` from the project root on every CLI entrypoint.

    Without this, ``SMART_MONEY_LIVE_TRADE`` and the rest of the
    SMART_MONEY_* switches are only honoured when the operator exports
    them by hand — the LaunchAgent-managed ``serve`` / ``run`` plists
    don't inherit the shell's dotenv state.  We make ``load_dotenv``
    idempotent (it never overrides an existing env var) so explicit
    shell exports still win.
    """
    try:
        from dotenv import load_dotenv  # type: ignore
    except ImportError:  # pragma: no cover — python-dotenv is in the venv
        return
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)

logger = logging.getLogger("smart_money")


@dataclass
class CliResult:
    job_name: str
    status: str
    started_at: datetime
    finished_at: datetime
    rows_seen: int
    rows_written: int
    summary: dict


def _record_run(result: CliResult) -> None:
    with get_engine().begin() as conn:
        conn.execute(
            CollectionRun.__table__.insert().values(
                job_name=result.job_name,
                status=result.status,
                started_at=result.started_at,
                finished_at=result.finished_at,
                rows_seen=result.rows_seen,
                rows_written=result.rows_written,
                error=None,
                details=result.summary,
            )
        )


def _run_collector(
    session: Session,
    client: PolymarketReadClient,
    settings: SmartMoneySettings,
    job_name: str,
    *,
    wallet_limit: int | None,
) -> dict:
    from .models import TrackedWalletSnapshot
    from sqlalchemy import delete as sa_delete

    collector = SmartMoneyCollector(session, client, settings)
    if job_name == "leaderboard":
        collector.collect_leaderboard()
        return {"job": "leaderboard"}
    if job_name == "markets":
        wallets = collector._select_tracked_wallets(wallet_limit)
        return collector.collect_market_metadata(wallets)
    if job_name == "trades":
        # Authoritative wallet selection: trades is the primary high-frequency job.
        wallets = collector._select_tracked_wallets(wallet_limit)
        # Persist the snapshot so other jobs (positions) use the exact same set.
        # The PK is (job_name, wallet), so we DELETE-then-INSERT. We flush after
        # the DELETE so the prior rows are actually gone from the database before
        # the new batch is INSERTed; without the explicit flush, the DELETE and
        # INSERT can be reordered in the same unit-of-work batch and the INSERT
        # trips a duplicate-key violation against rows the DELETE hadn't yet
        # removed (we observed this in production on 2026-07-20).
        result = session.execute(
            sa_delete(TrackedWalletSnapshot).where(
                TrackedWalletSnapshot.job_name == "trades"
            )
        )
        logger.info("trades: snapshot DELETE rowcount=%s", result.rowcount)
        session.flush()
        seen: set[str] = set()
        snapshot_rows = []
        for i, w in enumerate(wallets):
            if w in seen:
                logger.warning("trades: dropping duplicate wallet %s at position %d", w, i + 1)
                continue
            seen.add(w)
            snapshot_rows.append(TrackedWalletSnapshot(job_name="trades", wallet=w, rank=i + 1))
        session.add_all(snapshot_rows)
        session.flush()
        activity_summary = collector.collect_activity(wallets)
        # Phase 2: scoring + signal detection + risk filter run on the same session
        # so they see the freshly written trades / closed_positions.
        from .scoring import compute_trader_profiles
        from .signals import detect_signals
        from .risk import evaluate as risk_evaluate

        try:
            scoring_summary = compute_trader_profiles(
                session, lookback_days=settings.activity_lookback_days
            )
        except Exception:
            logger.exception("scoring failed")
            scoring_summary = {"scored": 0, "eligible": 0}
        try:
            from .window_scoring import recompute_window_scores
            window_summary = recompute_window_scores(session, window_days=30)
            scoring_summary.update({f"window_{k}": v for k, v in window_summary.items()})
        except Exception:
            logger.exception("window scoring failed")
        try:
            signal_summary = detect_signals(session, settings)
        except Exception:
            logger.exception("signals failed")
            signal_summary = {"new_open": 0, "consensus": 0, "written": 0}
        try:
            risk_summary = risk_evaluate(session, settings)
        except Exception:
            logger.exception("risk failed")
            risk_summary = {"pass": 0, "shrink": 0, "block": 0, "updated": 0}

        summary = {
            **activity_summary,
            "scored": scoring_summary.get("scored", 0),
            "eligible": scoring_summary.get("eligible", 0),
            "signals_new_open": signal_summary.get("new_open", 0),
            "signals_consensus": signal_summary.get("consensus", 0),
            "risk_pass": risk_summary.get("pass", 0),
            "risk_shrink": risk_summary.get("shrink", 0),
            "risk_block": risk_summary.get("block", 0),
        }
        return summary
    if job_name == "positions":
        # Reuse the latest trades snapshot to keep coverage aligned across jobs.
        snap_rows = (
            session.query(TrackedWalletSnapshot)
            .filter(TrackedWalletSnapshot.job_name == "trades")
            .order_by(TrackedWalletSnapshot.rank)
            .all()
        )
        if snap_rows:
            wallets = [r.wallet for r in snap_rows]
            snap_age = utc_now() - snap_rows[0].recorded_at
            if snap_age > timedelta(minutes=15):
                logger.warning(
                    "positions: trades snapshot is stale (age=%s); using it anyway",
                    snap_age,
                )
        else:
            logger.info("positions: no trades snapshot yet, falling back to direct select")
            wallets = collector._select_tracked_wallets(wallet_limit)
        return collector.collect_current_positions(wallets)
    if job_name == "all":
        return collector.run_all(wallet_limit=wallet_limit)
    raise ValueError(f"Unknown job: {job_name}")


def cmd_init_db(_: argparse.Namespace) -> int:
    create_schema()
    logger.info("schema ensured")
    return 0


def cmd_manual_follow(args: argparse.Namespace) -> int:
    """Add / remove / list manually-curated follow-list overrides.

    Sub-commands::

        smart_money manual-follow add --wallet 0x... [--username ...] [--note ...]
        smart_money manual-follow remove --wallet 0x...
        smart_money manual-follow list

    Adding a wallet inserts a row into ``smart_money_manual_follow`` and
    immediately re-runs :func:`refresh_follow_list` so the new entry
    shows up in :class:`FollowListEntry` with ``source='manual'``.

    Removing is the inverse — the wallet will then be re-evaluated by
    the auto filter and dropped if it fails the stability gates.
    """
    from .db import session_scope
    from .followlist import refresh_follow_list, get_follow_list
    from .models import ManualFollow

    action = args.action

    if action == "list":
        with session_scope() as session:
            rows = session.query(ManualFollow).order_by(ManualFollow.added_at.desc()).all()
            if not rows:
                print("(no manual follow entries)")
                return 0
            for m in rows:
                added = m.added_at.isoformat() if m.added_at else "—"
                print(f"  {m.wallet}  user={m.username or '—'}  added={added}")
                if m.note:
                    print(f"    note: {m.note}")
        return 0

    if not args.wallet:
        print("--wallet is required for add/remove", file=sys.stderr)
        return 2

    wallet = args.wallet.strip().lower()
    if not wallet.startswith("0x") or len(wallet) < 10:
        print(f"bad wallet address: {wallet!r}", file=sys.stderr)
        return 2

    with session_scope() as session:
        if action == "add":
            existing = session.get(ManualFollow, wallet)
            if existing:
                if args.username:
                    existing.username = args.username
                if args.note:
                    existing.note = args.note
                print(f"updated existing manual entry: {wallet}")
            else:
                session.add(ManualFollow(
                    wallet=wallet,
                    username=args.username,
                    note=args.note,
                ))
                print(f"added manual follow: {wallet}")
            session.commit()
        elif action == "remove":
            existing = session.get(ManualFollow, wallet)
            if not existing:
                print(f"not in manual-follow: {wallet}")
                return 1
            session.delete(existing)
            session.commit()
            print(f"removed manual follow: {wallet}")
        else:
            print(f"unknown action: {action}", file=sys.stderr)
            return 2

        # Always refresh the materialised follow list so the change
        # takes effect immediately (not on the next tick).
        try:
            refresh_follow_list(session, settings=get_settings())
        except Exception as exc:  # noqa: BLE001
            logger.warning("refresh_follow_list after manual edit failed: %s", exc)
        items = get_follow_list(session, top_n=settings.follow_list_max) if False else get_follow_list(session)
        manual_count = sum(1 for it in items if it.get("source") == "manual")
        print(f"follow_list_size={len(items)} (manual={manual_count})")
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    """Wipe the ``smart_money_follow_orders``, ``smart_money_signals``,
    ``smart_money_risk_runs`` and ``smart_money_follow_list`` tables so
    the next collection cycle starts from a clean slate.

    By default this is a *dry run*: it only prints the counts that
    would be removed.  Pass ``--yes`` to actually TRUNCATE.

    It also resets the in-process OrderEventBus snapshot.

    Why this exists
    ---------------
    During local development / manual testing, lots of fake orders
    (``signal_id=0`` / ``0xCONDTEST`` / ``0xABCD``) accumulate in the
    audit log.  Those rows have no ``Market`` slug so the dashboard's
    "查看" button 404s.  This command clears them so the next follower
    tick re-populates with real data.
    """
    from .config import SmartMoneySettings
    from .models import (
        FollowListEntry,
        FollowOrder,
        RiskFilterRun,
        Signal,
        WindowScore,
    )
    from api import get_order_event_bus, reset_default_order_event_bus

    if not args.yes:
        with session_scope() as session:
            counts = {
                "smart_money_follow_orders": session.execute(
                    text("SELECT COUNT(*) FROM smart_money_follow_orders")
                ).scalar() or 0,
                "smart_money_signals": session.execute(
                    text("SELECT COUNT(*) FROM smart_money_signals")
                ).scalar() or 0,
                "smart_money_risk_runs": session.execute(
                    text("SELECT COUNT(*) FROM smart_money_risk_runs")
                ).scalar() or 0,
                "smart_money_follow_list": session.execute(
                    text("SELECT COUNT(*) FROM smart_money_follow_list")
                ).scalar() or 0,
                "smart_money_window_scores": session.execute(
                    text("SELECT COUNT(*) FROM smart_money_window_scores")
                ).scalar() or 0,
                "smart_money_trader_scores": session.execute(
                    text("SELECT COUNT(*) FROM smart_money_trader_scores")
                ).scalar() or 0,
                "smart_money_order_events_in_bus": 0,  # in-memory, see below
            }
        logger.warning(
            "purge (DRY-RUN): would truncate %s — re-run with --yes to confirm",
            counts,
        )
        return 0

    with session_scope() as session:
        counts: dict[str, int] = {}
        for table in [
            "smart_money_follow_orders",
            "smart_money_signals",
            "smart_money_risk_runs",
            "smart_money_follow_list",
            "smart_money_window_scores",
        ]:
            counts[table] = session.execute(
                text(f"SELECT COUNT(*) FROM {table}")
            ).scalar() or 0
        # Wipe everything in one transaction.
        session.execute(FollowOrder.__table__.delete())
        session.execute(Signal.__table__.delete())
        session.execute(RiskFilterRun.__table__.delete())
        session.execute(FollowListEntry.__table__.delete())
        session.execute(WindowScore.__table__.delete())

    # Reset the bus so SSE subscribers stop receiving old dry-run events
    # on reconnect.
    try:
        bus = get_order_event_bus()
        with bus._lock:  # type: ignore[attr-defined]
            bus._snapshot.clear()  # type: ignore[attr-defined]
            bus._latest_by_order.clear()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    reset_default_order_event_bus()

    logger.warning("purge (committed): truncated %s", counts)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    # Apply runtime overrides to settings before any collector instantiation.
    # SmartMoneySettings is frozen — build a new instance with dataclasses.replace.
    import dataclasses
    settings = get_settings()
    overrides: dict = {}
    if args.live_trade:
        overrides["live_trade"] = True
    if args.feishu_webhook:
        overrides["feishu_webhook_url"] = args.feishu_webhook
    if overrides:
        settings = dataclasses.replace(settings, **overrides)
    if args.loop:
        return _cmd_run_loop(args, settings)
    return _cmd_run_once(args, settings)


def _cmd_run_once(args: argparse.Namespace, settings: SmartMoneySettings | None = None) -> int:
    settings = settings or get_settings()
    started = datetime.now(timezone.utc)
    summary, status, error = _execute_job(args.job, args.wallet_limit, settings)
    finished = datetime.now(timezone.utc)
    rows_written = sum(int(v) for k, v in summary.items() if isinstance(v, (int, float)) and k != "job" and k != "tracked_wallets")
    result = CliResult(
        job_name=args.job,
        status=status,
        started_at=started,
        finished_at=finished,
        rows_seen=0,
        rows_written=rows_written,
        summary={"summary": summary, "error": error},
    )
    try:
        _record_run(result)
    except Exception:
        logger.exception("failed to persist run history")
    print(json.dumps(summary, indent=2, default=str))
    return 0 if status == "ok" else 1


def _execute_job(
    job_name: str,
    wallet_limit: int | None,
    settings: SmartMoneySettings | None = None,
) -> tuple[dict, str, str | None]:
    """Run one collector job. Returns (summary, status, error)."""
    settings = settings or get_settings()
    summary: dict = {"job": job_name}
    status = "ok"
    error: str | None = None
    try:
        if job_name == "follow":
            with session_scope() as session:
                summary = _run_follow_tick(session, settings)
                # Also emit monitoring events for any new trades from
                # follow-list wallets (manual + auto).  This is what
                # populates the "实时跟单事件" panel for the operator's
                # pinned wallets — the executor's consensus path only
                # fires when 2+ wallets agree on the same condition,
                # which is rare for manually-pinned traders.
                announced = _announce_follow_list_trades(session)
                if announced:
                    logger.info("follow-list trade tick: announced %s events", announced)
                trimmed = _maybe_trim_order_events(session)
                if trimmed:
                    logger.info("trimmed %s old OrderEventLog rows", trimmed)
        else:
            with PolymarketReadClient(settings) as client:
                with session_scope() as session:
                    summary = _run_collector(session, client, settings, job_name, wallet_limit=wallet_limit)
    except Exception as exc:
        status = "error"
        error = repr(exc)
        logger.exception("collector job %s failed", job_name)
    return summary, status, error


def _run_follow_tick(session, settings: SmartMoneySettings) -> dict:
    """One follow-loop iteration: refresh follow list, check for new pass signals."""
    from .followlist import refresh_follow_list, get_follow_list
    from .notifier import send_dry_run_alert
    from .executor import execute as exec_order
    from .models import Signal

    top_wallets = refresh_follow_list(session, settings)
    follow_rows = get_follow_list(session, top_n=settings.follow_list_max)

    # Find pass+consensus signals that haven't been executed yet.
    # Time window is generous (24h) because the trades tick that produced
    # them may have run minutes ago; the follow-tick should always catch up.
    from .models import FollowOrder as _FO
    cutoff = utc_now() - timedelta(hours=24)
    candidates = (
        session.query(Signal)
        .outerjoin(_FO, _FO.signal_id == Signal.id)
        .filter(
            Signal.status == "pass",
            Signal.signal_type == "consensus",
            Signal.trader_count >= settings.follow_min_consensus_for_execute,
            Signal.created_at >= cutoff,
            _FO.id.is_(None),  # not yet processed
        )
        .order_by(Signal.created_at.desc())
        .limit(20)
        .all()
    )

    notified = 0
    executed = 0
    dry_run = 0
    for sig in candidates:
        # Skip if we already have a FollowOrder for this signal
        from .models import FollowOrder as _FO
        existing = (
            session.query(_FO).filter(_FO.signal_id == sig.id).first()
        )
        if existing:
            continue

        sig_dict = {
            "id": sig.id,
            "signal_type": sig.signal_type,
            "direction": sig.direction,
            "condition_id": sig.condition_id,
            "trigger_wallets": sig.trigger_wallets or [],
            "trader_count": sig.trader_count,
            "confidence": sig.confidence,
            "suggested_size_usdc": sig.suggested_size_usdc,
            "risk_reasons": sig.risk_reasons,
            "title": sig.title,
        }
        if settings.feishu_webhook_url:
            ok, resp = send_dry_run_alert(
                settings.feishu_webhook_url,
                sig_dict,
            )
            if ok:
                notified += 1
            else:
                logger.warning("feishu push failed signal=%s: %s", sig.id, resp)

        # Always process through the executor (dry-run by default).
        result = exec_order(session, settings, signal_id=sig.id)
        if result.get("status") == "submitted":
            executed += 1
        elif result.get("status") == "dry_run":
            dry_run += 1

    return {
        "follow_list_size": len(follow_rows),
        "top_wallets": top_wallets,
        "candidates": len(candidates),
        "notified": notified,
        "executed": executed,
        "dry_run": dry_run,
    }


# Bound for the persisted OrderEventLog table.  Older rows are pruned
# by the follow-tick every 6 hours so the table stays small.  We could
# also wire this into a periodic cron, but piggybacking on the follow
# loop keeps everything in one process.
_ORDER_EVENT_LOG_TRIM_AFTER_HOURS = 24
_ORDER_EVENT_LOG_TRIM_EVERY_TICKS = 12  # 12 * 30s ≈ 6min
_order_event_tick_counter = 0


def _maybe_trim_order_events(session) -> int:
    """Prune old OrderEventLog rows.  Returns the rowcount pruned.

    Runs every ``_ORDER_EVENT_LOG_TRIM_EVERY_TICKS`` ticks (≈6 minutes)
    so it never becomes a hot path.  Older rows are kept for at least
    24h before deletion, which is plenty of time for the dashboard to
    catch up after a restart.
    """
    global _order_event_tick_counter
    _order_event_tick_counter += 1
    if _order_event_tick_counter < _ORDER_EVENT_LOG_TRIM_EVERY_TICKS:
        return 0
    _order_event_tick_counter = 0
    try:
        from .models import OrderEventLog
    except ImportError:  # pragma: no cover
        return 0
    cutoff = utc_now() - timedelta(hours=_ORDER_EVENT_LOG_TRIM_AFTER_HOURS)
    return session.query(OrderEventLog).filter(
        OrderEventLog.created_at < cutoff
    ).delete(synchronize_session=False)


# ---------------------------------------------------------------------------
# Follow-list trade scan
# ---------------------------------------------------------------------------
#
# The executor's follow-tick only runs on *consensus* signals (2+ tracked
# traders agreeing on the same condition).  Manually pinned wallets
# rarely form consensus because they trade on different markets, so the
# user previously saw the "实时跟单事件" panel empty for the wallets they
# explicitly cared about.  To fix that we run a separate scan on every
# follow tick: pull all recent BUY trades from any wallet currently on
# the follow-list (manual or auto) and emit a "mirrored" OrderEvent per
# trade so the dashboard surfaces real-time leader activity.
#
# This scan only publishes events — it does NOT place orders.  Order
# placement still goes through ``executor.execute()`` (consensus path)
# or the manual-order endpoint (semi-auto path), so there is no extra
# surface for accidental trades.
_FOLLOW_TRADE_SCAN_LOOKBACK_MINUTES = 5
_FOLLOW_TRADE_SCAN_WATERMARK_FLOOR_MIN = 5  # never scan older than this
_FOLLOW_TRADE_SCAN_LOOKBACK_HOURS = 24  # how far back to scan for new trades
_FOLLOW_TRADE_SCAN_PER_WALLET_LIMIT = 50  # cap on trades announced per wallet per tick
# Cache the trade fingerprints we have already announced so we don't
# spam the dashboard with the same trade on every tick.
_announced_trade_fingerprints: set[str] = set()


def _announce_follow_list_trades(session) -> int:
    """Publish ``mirrored`` OrderEvents for new trades from follow-list wallets.

    Iterates the current ``smart_money_follow_list`` (plus any manually
    pinned wallets), pulls every trade in the last 24h, and emits a
    ``mirrored`` event for any trade the dashboard hasn't already seen.
    The DB upsert (by ``order_id = "fol-" + fingerprint``) collapses any
    cross-restart re-emit so the operator always sees each trade
    exactly once.

    Returns the number of new events published.
    """
    global _announced_trade_fingerprints
    try:
        from .followlist import get_follow_list
        from .models import ManualFollow, OrderEventLog, Trade
        from api.order_events import OrderEvent, OrderStatus, get_order_event_bus
    except ImportError:  # pragma: no cover
        return 0

    # Get current follow-list wallets (manual + auto).
    follow_rows = get_follow_list(session, top_n=200)
    follow_wallets = {r["wallet"] for r in follow_rows if r.get("wallet")}
    # Also include any wallet the operator has manually pinned even if
    # the auto follow-list doesn't include them yet (e.g. the wallet is
    # newly pinned and the follow-list hasn't refreshed).
    try:
        manual_rows = session.query(ManualFollow.wallet).all()
        follow_wallets.update({r.wallet for r in manual_rows if r.wallet})
    except Exception:  # pragma: no cover
        pass
    if not follow_wallets:
        return 0

    # Cross-restart safe scan: pull every trade in the last 24h for
    # these wallets, then dedup against ``OrderEventLog`` (by order_id
    # ``fol-{fp[:24]}``) so we never re-publish a trade the dashboard
    # has already seen.  This means:
    #   * On every worker restart we re-scan the 24h window.  The DB
    #     upsert (by order_id) collapses any re-emit so the dashboard
    #     sees each trade exactly once.
    #   * If the collector stops fetching new trades for a wallet,
    #     ``Trader.last_active_at`` stays put and we keep re-announcing
    #     the same trades on every tick — but the DB dedup prevents
    #     flooding the dashboard.
    cutoff = utc_now() - timedelta(hours=_FOLLOW_TRADE_SCAN_LOOKBACK_HOURS)
    rows = (
        session.query(Trade)
        .filter(
            Trade.wallet.in_(follow_wallets),
            Trade.traded_at >= cutoff,
        )
        .order_by(Trade.traded_at.desc())
        .limit(_FOLLOW_TRADE_SCAN_PER_WALLET_LIMIT * max(1, len(follow_wallets)))
        .all()
    )
    if not rows:
        return 0

    # Build a set of trade fingerprints that already have an event row
    # so we don't re-publish on every tick.  We use a single IN-query
    # against OrderEventLog for performance.
    fps = [t.fingerprint for t in rows if t.fingerprint]
    if not fps:
        return 0
    expected_order_ids = [f"fol-{f[:24]}" for f in fps]
    existing_rows = (
        session.query(OrderEventLog.order_id)
        .filter(OrderEventLog.order_id.in_(expected_order_ids))
        .all()
    )
    already_published = {
        r.order_id[len("fol-"):]
        for r in existing_rows
        if r.order_id and r.order_id.startswith("fol-")
    }

    # Cap the in-process cache so a long-running worker doesn't leak memory.
    if len(_announced_trade_fingerprints) > 10_000:
        _announced_trade_fingerprints.clear()

    bus = get_order_event_bus()
    published = 0
    for trade in rows:
        if not trade.fingerprint or trade.fingerprint in _announced_trade_fingerprints:
            continue
        if trade.fingerprint[:24] in already_published:
            continue
        ev = OrderEvent(
            event_id="",
            order_id=f"fol-{trade.fingerprint[:24]}",
            leader_wallet=trade.wallet,
            market_id=trade.condition_id,
            asset_id=trade.token_id,
            side=trade.side,
            status=OrderStatus.MIRRORED,
            reason=(
                f"leader {trade.side.lower()} · {trade.outcome or ''} "
                f"· {trade.title or (trade.condition_id or '')[:12]}"
            ).strip(),
            data={
                "price": float(trade.price or 0),
                "size": float(trade.size or 0),
                "amount": float(trade.amount or 0),
                "title": trade.title,
                "slug": trade.slug,
                "event_slug": getattr(trade, "event_slug", None),
                "traded_at": trade.traded_at.isoformat() if trade.traded_at else None,
                "outcome": trade.outcome,
                "kind": "follow-list-trade",
            },
        )
        try:
            bus.publish(ev)
            _announced_trade_fingerprints.add(trade.fingerprint)
            published += 1
        except Exception:  # pragma: no cover — defensive
            logger.exception("publish follow-list trade event failed fp=%s", trade.fingerprint)
    if published:
        logger.info("follow-list trade tick: announced %s events", published)
    return published


# Per-job default intervals (seconds). Override via --loop-*-seconds flags.
LOOP_DEFAULTS: dict[str, int] = {
    "leaderboard": 24 * 60 * 60,
    "markets": 6 * 60 * 60,
    "trades": 5 * 60,
    "positions": 5 * 60,
}


def _record_loop_iteration(job_name: str, status: str, started: datetime, finished: datetime,
                            summary: dict, error: str | None) -> None:
    rows_written = sum(int(v) for k, v in summary.items() if isinstance(v, (int, float)) and k != "job" and k != "tracked_wallets")
    result = CliResult(
        job_name=job_name,
        status=status,
        started_at=started,
        finished_at=finished,
        rows_seen=0,
        rows_written=rows_written,
        summary={"summary": summary, "error": error},
    )
    try:
        _record_run(result)
    except Exception:
        logger.exception("failed to persist run history")


def _cmd_run_loop(args: argparse.Namespace, settings: SmartMoneySettings | None = None) -> int:
    settings = settings or get_settings()
    """Run the collector forever. No external scheduler required.

    Scheduling strategy
    -------------------
    On startup:
      - leaderboard runs immediately (cheap, ~30s).
      - trades + positions run immediately (need fresh data).
      - markets is deferred by `--loop-markets-initial-delay` seconds (default
        60s) so it doesn't block the cheap jobs. markets is the slowest job
        (it has to enrich every condition_id we see), so it gets its own
        independent schedule.

    After the first cycle, each job runs on its own period. Jobs run sequentially
    in the main loop, but since leaderboard/markets are infrequent (>=1h) and
    trades/positions are short, this is fine for Phase 1.
    SIGINT/SIGTERM cleanly stop the loop.
    """
    intervals: dict[str, int] = {
        "leaderboard": args.loop_leaderboard_seconds or LOOP_DEFAULTS["leaderboard"],
        "markets": args.loop_markets_seconds or LOOP_DEFAULTS["markets"],
        "trades": args.loop_trades_seconds or LOOP_DEFAULTS["trades"],
        "positions": args.loop_positions_seconds or LOOP_DEFAULTS["positions"],
        "follow": args.loop_follow_seconds,
    }
    tick_seconds = max(1, args.loop_tick_seconds)
    markets_initial_delay = max(0, args.loop_markets_initial_delay)

    # First-cycle ordering: kick off cheap jobs first (leaderboard, follow),
    # then the long-running ones (trades, positions) so follow can start
    # mirroring signals while the data pipeline finishes.
    first_cycle: list[str] = ["follow", "leaderboard", "trades", "positions"]
    subsequent_order: list[str] = ["follow", "trades", "positions", "leaderboard", "markets"]

    stop_event = threading.Event()

    def _request_stop(signum, frame):  # noqa: ARG001
        logger.info("received signal %s, shutting down loop", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    now = time.monotonic()
    next_run: dict[str, float] = {
        "follow": now,  # start mirror immediately on boot
        "leaderboard": now + 5,
        "markets": now + markets_initial_delay,
        "trades": now + 10,  # wait a moment so follow can grab the boot signals
        "positions": now + 10,
    }
    logger.info(
        "loop mode: job=%s intervals=%s(s) tick=%ds markets_initial_delay=%ds wallet_limit=%s live_trade=%s",
        args.job, intervals, tick_seconds, markets_initial_delay, args.wallet_limit, args.live_trade,
    )
    if settings.live_trade:
        logger.warning(
            "LIVE TRADE ENABLED: funder=%s sig_type=%s caps: per_order=$%s daily=$%s concurrent_per_cond=%s",
            settings.polymarket_funder or "<MISSING>",
            settings.polymarket_signature_type,
            settings.live_default_size_usdc,
            settings.live_max_daily_usdc,
            settings.live_max_concurrent_per_condition,
        )
    else:
        logger.info("LIVE TRADE DISABLED: dry-run only (set SMART_MONEY_LIVE_TRADE=1 to enable)")

    first_cycle_done = False
    while not stop_event.is_set():
        now = time.monotonic()

        order = first_cycle if not first_cycle_done else subsequent_order
        for job in order:
            if now < next_run[job]:
                continue
            started = datetime.now(timezone.utc)
            logger.info("loop tick: running job=%s", job, extra={"job": job})
            summary, status, error = _execute_job(job, args.wallet_limit, settings)
            finished = datetime.now(timezone.utc)
            _record_loop_iteration(job, status, started, finished, summary, error)
            logger.info(
                "loop tick: job=%s status=%s rows=%s",
                job, status, {k: v for k, v in summary.items() if k != "job"},
            )
            next_run[job] = time.monotonic() + intervals[job]

        first_cycle_done = True

        # Sleep in small slices so SIGINT is honoured quickly.
        sleep_for = min(tick_seconds, max(0.0, min(next_run.values()) - time.monotonic()))
        if sleep_for > 0:
            stop_event.wait(sleep_for)

        # Heartbeat: write a single line every ~25s so an operator can
        # verify the loop is alive without scrolling through job noise.
        log_heartbeat("idle", next_in=sleep_for)

    logger.info("loop mode stopped cleanly")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .dashboard_app import get_app
    get_app()
    uvicorn.run(
        "smart_money.dashboard_app:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        log_config=_uvicorn_log_config(args.log_level),
    )
    return 0


def _uvicorn_log_config(level: str) -> dict:
    """uvicorn log config that stamps every line with millisecond precision.

    Output sample:
        2026-07-22 23:15:42.123 [INFO ] uvicorn.error: Started server process [12345]
        2026-07-22 23:15:42.456 [INFO ] uvicorn.access: 127.0.0.1:50694 - "GET /api/dashboard HTTP/1.1" 200 OK
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
            "access": {
                "format": "%(asctime)s.%(msecs)03d [ACCESS] %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
                "formatter": "default",
            },
            "access": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": "access",
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": level.upper(), "propagate": False},
            "uvicorn.error": {"handlers": ["default"], "level": level.upper(), "propagate": False},
            "uvicorn.access": {"handlers": ["access"], "level": level.upper(), "propagate": False},
            "smart_money": {"handlers": ["default"], "level": level.upper(), "propagate": False},
            "httpx": {"level": "WARNING"},
            "httpcore": {"level": "WARNING"},
        },
    }


def cmd_snapshot(_: argparse.Namespace) -> int:
    settings = get_settings()
    with session_scope() as session:
        snap = SmartMoneyAnalytics(session, settings).dashboard_snapshot()
    print(json.dumps(snap, indent=2, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="smart_money", description="Polymarket Smart Money Tracker (Phase 1, read-only)")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init-db", help="Create smart_money_* tables in PostgreSQL")
    init_p.set_defaults(func=cmd_init_db)

    purge_p = sub.add_parser(
        "purge",
        help=(
            "Wipe follow_orders / signals / risk_runs / follow_list so the next "
            "follow tick re-populates with real data"
        ),
    )
    purge_p.add_argument(
        "--yes",
        action="store_true",
        help="Actually TRUNCATE (default: dry-run report)",
    )
    purge_p.set_defaults(func=cmd_purge)

    run_p = sub.add_parser("run", help="Run a single collector job (or loop forever with --loop)")
    run_p.add_argument(
        "--job",
        required=True,
        choices=["leaderboard", "markets", "trades", "positions", "all"],
    )
    run_p.add_argument("--wallet-limit", type=int, default=None)
    run_p.add_argument(
        "--loop",
        action="store_true",
        help="Run the pipeline forever in this process (no extra scheduler needed). "
             "Each job runs on its own interval; SIGINT/SIGTERM cleanly stops.",
    )
    run_p.add_argument(
        "--loop-leaderboard-seconds",
        type=int,
        default=0,
        help="Loop interval for leaderboard job (default 86400 = 24h)",
    )
    run_p.add_argument(
        "--loop-markets-seconds",
        type=int,
        default=0,
        help="Loop interval for markets job (default 21600 = 6h)",
    )
    run_p.add_argument(
        "--loop-trades-seconds",
        type=int,
        default=0,
        help="Loop interval for trades job (default 300 = 5min)",
    )
    run_p.add_argument(
        "--loop-positions-seconds",
        type=int,
        default=0,
        help="Loop interval for positions job (default 300 = 5min)",
    )
    run_p.add_argument(
        "--loop-tick-seconds",
        type=int,
        default=10,
        help="How often the loop wakes up to check schedule (default 10s)",
    )
    run_p.add_argument(
        "--loop-markets-initial-delay",
        type=int,
        default=60,
        help="Delay the first markets tick by N seconds so cheap jobs (trades/positions) "
             "run first (default 60s)",
    )
    run_p.add_argument(
        "--loop-follow-seconds",
        type=int,
        default=30,
        help="Loop interval for follow (signal+executor) job (default 30s)",
    )
    run_p.add_argument(
        "--live-trade",
        action="store_true",
        help="REAL order placement via CLOB. Default is dry-run (logs only, no orders).",
    )
    run_p.add_argument(
        "--feishu-webhook",
        default=None,
        help="Override Feishu webhook URL (otherwise from SMART_MONEY_FEISHU_WEBHOOK_URL)",
    )
    run_p.set_defaults(func=cmd_run)

    serve_p = sub.add_parser("serve", help="Start the FastAPI dashboard server")
    serve_p.add_argument("--host", default="0.0.0.0")
    serve_p.add_argument("--port", type=int, default=8088)
    serve_p.add_argument("--log-level", default="info")
    serve_p.set_defaults(func=cmd_serve)

    snap_p = sub.add_parser("snapshot", help="Print the dashboard snapshot to stdout as JSON")
    snap_p.set_defaults(func=cmd_snapshot)

    # ----- manual-follow -----
    mf_p = sub.add_parser(
        "manual-follow",
        help="Add / remove / list wallets pinned to the follow list by hand",
    )
    mf_sub = mf_p.add_subparsers(dest="action", required=True)
    mf_add = mf_sub.add_parser("add", help="Pin a wallet to the follow list")
    mf_add.add_argument("--wallet", required=True)
    mf_add.add_argument("--username", default=None)
    mf_add.add_argument("--note", default=None)
    mf_rm = mf_sub.add_parser("remove", help="Un-pin a wallet")
    mf_rm.add_argument("--wallet", required=True)
    mf_sub.add_parser("list", help="List every pinned wallet")
    mf_p.set_defaults(func=cmd_manual_follow)

    return parser


def main(argv: list[str] | None = None) -> int:
    _bootstrap_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.log_level)
    logger.info(
        "[BOOT] smart_money cli started cmd=%s pid=%s argv=%s",
        sys.argv[0], os.getpid(), sys.argv[1:],
    )
    return args.func(args)


_HEARTBEAT = {"last": 0.0, "uptime_started": time.monotonic()}


def _configure_logging(level: str) -> None:
    """Install a single root logger with a readable, always-timestamped format.

    Format:  ``2026-07-22 23:15:42.123 [INFO ] smart_money: <msg>``
    The trailing space after the level keeps columns aligned for grep.

    We also:
      - silence ``httpx`` to WARNING (its per-request INFO spam drowns out
        everything else in the log file)
      - silence ``httpcore`` similarly
      - tighten the level of a few noisy third-party loggers
    """
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Quiet the HTTP layer — one log line per outbound request is too much.
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Filter on the loop logger so that every loop tick gets a [TICK] /
    # [BOOT] / [JOB] / [HEARTBEAT] / [ERR] marker — makes it trivial to
    # scan the file for "is it still running?".
    loop_logger = logging.getLogger("smart_money")
    loop_logger.addFilter(_MarkerFilter())


class _MarkerFilter(logging.Filter):
    """Prefix loop-logger records with a tag so grep -E '\\(TICK|JOB|ERR\\)' works.

    Tag rules:
      - record starts with ``loop tick``                → [TICK]
      - record starts with ``loop mode`` / ``cli started`` → [BOOT]
      - job lines (status, running)                     → [JOB]
      - exception / traceback                           → [ERR]
      - heartbeat (``heartbeat:``)                      → [HEARTBEAT]
      - everything else                                  → []
    """

    def filter(self, record: logging.LogRecord) -> bool:  # type: ignore[override]
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if record.exc_info:
            tag = "[ERR] "
        elif msg.startswith("loop tick"):
            tag = "[TICK] "
        elif msg.startswith("loop mode") or msg.startswith("cli started"):
            tag = "[BOOT] "
        elif msg.startswith("heartbeat:"):
            tag = "[HEARTBEAT] "
        elif (
            msg.startswith("smart_money: loop tick")
            or msg.startswith("smart_money.")
        ):
            tag = "[JOB] "
        else:
            tag = ""
        if tag and not record.msg.startswith(tuple(
            f"[{t.strip('[] ')}] " for t in ("BOOT", "TICK", "JOB", "ERR", "HEARTBEAT")
        )):
            # Only the prefix; preserve any %-style formatting on the original msg.
            record.msg = tag + str(record.msg)
        return True


def log_heartbeat(stage: str, *, next_in: float | None = None) -> None:
    """Emit a single [HEARTBEAT] line so an idle operator can see it's alive."""
    now = time.time()
    if now - _HEARTBEAT["last"] < 25:
        return  # throttle to once per 25s
    _HEARTBEAT["last"] = now
    uptime = int(time.monotonic() - _HEARTBEAT["uptime_started"])
    hh, rem = divmod(uptime, 3600)
    mm, ss = divmod(rem, 60)
    suffix = f" next_in={int(next_in)}s" if next_in is not None else ""
    logger.info(
        "heartbeat: stage=%s pid=%s uptime=%02d:%02d:%02d%s",
        stage, os.getpid(), hh, mm, ss, suffix,
    )


if __name__ == "__main__":
    sys.exit(main())
