from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func as sql_func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql import Insert

from .client import PolymarketReadClient
from .config import SmartMoneySettings
from .models import (
    ClosedPosition,
    CurrentPosition,
    LeaderboardEntry,
    ManualFollow,
    Market,
    PositionSnapshot,
    Trader,
    Trade,
)
from .normalization import (
    as_bool,
    as_datetime,
    as_decimal,
    as_int,
    as_json_list,
    stable_fingerprint,
    utc_now,
)

logger = logging.getLogger(__name__)


def _cid(row: dict) -> str:
    cid = row.get("conditionId") or row.get("condition_id") or row.get("conditionID")
    return str(cid).strip().lower() if cid else ""


def _dedup_payloads(payloads: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    seen: set[Any] = set()
    out: list[dict[str, Any]] = []
    for p in payloads:
        val = p.get(key)
        if val is not None and val not in seen:
            seen.add(val)
            out.append(p)
    return out


# PostgreSQL caps a single statement at 65535 bind parameters. Each row in `payloads`
# contributes len(payloads[0]) params, so we chunk the insert to stay well below that.
MAX_PARAMS_PER_STMT = 60_000


def _upsert(session: Session, table, payloads: list[dict[str, Any]], index_cols: list[str], update_cols: list[str]) -> None:
    if not payloads:
        return
    if not isinstance(payloads, list) or not isinstance(payloads[0], dict):
        raise TypeError("payloads must be a list of dicts")
    cols_per_row = len(payloads[0])
    if cols_per_row <= 0:
        return
    rows_per_chunk = max(1, MAX_PARAMS_PER_STMT // cols_per_row)
    for chunk_start in range(0, len(payloads), rows_per_chunk):
        chunk = payloads[chunk_start:chunk_start + rows_per_chunk]
        stmt: Insert = pg_insert(table).values(chunk)
        update = {col: getattr(stmt.excluded, col) for col in update_cols}
        session.execute(
            stmt.on_conflict_do_update(index_elements=index_cols, set_=update)
        )


class SmartMoneyCollector:
    """Populate smart-money tables from public Polymarket Data/Gamma APIs (read-only)."""

    LEADERBOARD_CATEGORIES: tuple[tuple[str, str], ...] = (
        ("OVERALL", "ALL"),
        ("OVERALL", "MONTH"),
        ("OVERALL", "WEEK"),
        ("OVERALL", "DAY"),
    )

    def __init__(
        self,
        session: Session,
        client: PolymarketReadClient,
        settings: SmartMoneySettings,
    ) -> None:
        self.session = session
        self.client = client
        self.settings = settings

    # ------------------------------------------------------------------
    # Full pipeline
    # ------------------------------------------------------------------

    def run_all(self, *, wallet_limit: int | None = None) -> dict[str, int]:
        result = {
            "leaderboard_rows": 0,
            "tracked_wallets": 0,
            "markets": 0,
            "closed_positions": 0,
            "trades": 0,
            "positions": 0,
            "snapshots": 0,
        }
        self.collect_leaderboard()
        wallets = self._select_tracked_wallets(wallet_limit)
        result["tracked_wallets"] = len(wallets)
        logger.info("run_all: collected %d wallets", len(wallets))

        m1 = self.collect_market_metadata(wallets)
        result.update(m1)
        logger.info("run_all: market_metadata done, closed_positions=%d", result["closed_positions"])

        m2 = self.collect_activity(wallets)
        result["trades"] = m2["trades"]
        result["markets"] += m2["markets"]
        logger.info("run_all: activity done, trades=%d", result["trades"])

        m3 = self.collect_current_positions(wallets)
        result["positions"] = m3["positions"]
        result["snapshots"] = m3["snapshots"]
        result["markets"] += m3["markets"]
        logger.info("run_all: current_positions done, positions=%d", result["positions"])

        self._prune_stale_trackers()
        logger.info("run_all: DONE result=%s", result)
        return result

    # ------------------------------------------------------------------
    # Leaderboard
    # ------------------------------------------------------------------

    def collect_leaderboard(self) -> list[dict]:
        now = utc_now()
        wallets_merged: dict[str, Trader] = {}
        all_rows: list[LeaderboardEntry] = []

        for category, time_period in self.LEADERBOARD_CATEGORIES:
            if self.settings.top_trader_limit <= 50 and (category, time_period) != ("OVERALL", "ALL"):
                break
            entries = self.client.fetch_leaderboard(
                limit=self.settings.top_trader_limit,
                category=category,
                time_period=time_period,
                order_by="PNL",
            )
            for idx, entry in enumerate(entries, start=1):
                wallet = (entry.get("proxyWallet") or "").strip().lower()
                if not wallet:
                    continue
                rank = as_int(entry.get("rank"), default=idx) or idx
                if wallet not in wallets_merged:
                    wallets_merged[wallet] = Trader(
                        wallet=wallet,
                        username=entry.get("userName") or entry.get("name"),
                        pseudonym=entry.get("pseudonym"),
                        profile_image=entry.get("profileImage"),
                        x_username=entry.get("xUsername"),
                        verified=bool(entry.get("verifiedBadge")),
                        tracked=True,
                        last_active_at=now,
                        last_seen_at=now,
                    )
                all_rows.append(
                    LeaderboardEntry(
                        collected_at=now,
                        category=category,
                        time_period=time_period,
                        rank=rank,
                        wallet=wallet,
                        pnl=as_decimal(entry.get("pnl")),
                        volume=as_decimal(entry.get("vol")),
                        raw=entry,
                    )
                )

            # Flush new traders to DB before inserting entries (entries FK-reference traders)
            # Batch-check which wallets already exist (1 query instead of O(n))
            if wallets_merged:
                existing = {
                    row[0] for row in self.session.execute(
                        select(Trader.wallet).where(Trader.wallet.in_(list(wallets_merged.keys())))
                    ).all()
                }
                new_wallets = [t for w, t in wallets_merged.items() if w not in existing]
                if new_wallets:
                    self.session.add_all(new_wallets)
                    self.session.flush()

        # Batch upsert all leaderboard entries
        if all_rows:
            lb_payloads = []
            seen_keys: set[tuple] = set()
            for r in all_rows:
                key = (r.collected_at, r.category, r.time_period, r.wallet)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                lb_payloads.append({
                    "collected_at": r.collected_at,
                    "category": r.category,
                    "time_period": r.time_period,
                    "rank": r.rank,
                    "wallet": r.wallet,
                    "pnl": r.pnl,
                    "volume": r.volume,
                    "raw": r.raw,
                })
            if lb_payloads:
                stmt = pg_insert(LeaderboardEntry.__table__).values(lb_payloads)
                update = {
                    col: getattr(stmt.excluded, col)
                    for col in ("rank", "pnl", "volume", "raw")
                }
                self.session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["collected_at", "category", "time_period", "wallet"],
                        set_=update,
                    )
                )
        return [{"wallet": r.wallet, "category": r.category, "time_period": r.time_period} for r in all_rows]

    def _select_tracked_wallets(self, wallet_limit: int | None) -> list[str]:
        """Pick which wallets the per-tick collectors (trades, positions,
        activity) should fetch for.

        Historically this only considered the ALL-time leaderboard, which
        meant the per-period leaders (DAY / WEEK / MONTH) were *ingested*
        into ``smart_money_leaderboard_entries`` but never had their
        trades/positions fetched — so they never appeared in any
        ``WindowScore`` row, never in the dashboard's 30-day top list, and
        never in the follow list.  That's a blind spot for copy-trading
        because "today's winners" are by definition active right now.

        We now take the union of (ALL ∪ MONTH ∪ WEEK ∪ DAY) leaderboards
        and de-dup.  A wallet that appears in any of those rankings is
        worth tracking.  We rank by the strongest single-period PnL so the
        most profitable leaders come first when ``wallet_limit`` truncates.
        """
# (a) The set of wallets that have *ever* appeared on a public
        # leaderboard AND are flagged tracked.  Done as a subquery so we
        # can order by the *maximum* PnL across periods.
        lb_subq = (
            select(
                LeaderboardEntry.wallet.label("wallet"),
                sql_func.coalesce(sql_func.max(LeaderboardEntry.pnl), 0).label("max_pnl"),
                sql_func.count(sql_func.distinct(LeaderboardEntry.time_period)).label("period_count"),
            )
            .where(
                LeaderboardEntry.category == "OVERALL",
                LeaderboardEntry.time_period.in_(["ALL", "MONTH", "WEEK", "DAY"]),
            )
            .group_by(LeaderboardEntry.wallet)
            .subquery()
        )
        # Mix the two signals so traders appearing on short-period
        # boards (DAY/MONTH) get a chance even if they're not on the
        # ALL-time board.  ``score`` is a sum of period_count × weight
        # + max_pnl; pure winners on ALL still rank first because
        # their max_pnl dominates, but a wallet only on DAY with a
        # quarter-million PnL will beat a wallet only on ALL with $5K.
        # We pre-compute the score inside the inner subquery so the
        # outer SELECT only needs ``DISTINCT wallet`` — that lets us
        # ORDER BY the score column without the "ORDER BY must appear
        # in SELECT" error PostgreSQL raises on ``SELECT DISTINCT``.
        ranked = (
            select(
                lb_subq.c.wallet.label("wallet"),
                (lb_subq.c.max_pnl + lb_subq.c.period_count * 50000).label("score"),
            )
            .order_by((lb_subq.c.max_pnl + lb_subq.c.period_count * 50000).desc())
            .limit(wallet_limit or self.settings.tracked_wallet_limit)
            .subquery()
        )
        stmt = (
            select(ranked.c.wallet)
            .join(Trader, Trader.wallet == ranked.c.wallet)
            .where(Trader.tracked.is_(True))
            .distinct()
        )
        leaderboard_wallets = [row[0] for row in self.session.execute(stmt).all()] 
        # Operator-pinned wallets (ManualFollow) are always included even
        # if the wallet has not yet appeared on a public leaderboard.  This
        # is what makes "manually pin a wallet, get its trades / positions
        # updated on the next 5-minute tick" work end-to-end.
        # LEFT-JOIN ``Trader`` so a manual-followed wallet that
        # hasn't been ingested yet (no ``Trader`` row) is *still*
        # included.  We only skip it when it exists in ``Trader``
        # *and* ``tracked`` was explicitly set to False by the
        # operator.  This matches the design intent — manual
        # pinning must always work, even on the first tick after
        # pinning.
        manual_q = (
            select(ManualFollow.wallet)
            .outerjoin(Trader, Trader.wallet == ManualFollow.wallet)
            .where(or_(Trader.wallet.is_(None), Trader.tracked.is_(True)))
        )
        manual_wallets = [row[0] for row in self.session.execute(manual_q).all()]
        # Operator-pinned wallets are *first* in the merged list so they
        # get fetched before the circuit breaker can open on flaky
        # leaderboard RPCs.  This guarantees that a manual follow you
        # pinned a few minutes ago always has fresh positions, even
        # mid-Cloudflare instability.
        merged: list[str] = []
        seen: set[str] = set()
        for w in manual_wallets:
            if w in seen:
                continue
            seen.add(w)
            merged.append(w)
        for w in leaderboard_wallets:
            if w in seen:
                continue
            seen.add(w)
            merged.append(w)
        return merged

    # ------------------------------------------------------------------
    # Market metadata
    # ------------------------------------------------------------------

    def collect_market_metadata(self, wallets: list[str]) -> dict[str, int]:
        # NOTE: closed positions are now written by collect_activity (every 5min),
        # not here, so markets job is purely market enrichment.
        condition_ids = self._collect_condition_ids(wallets)
        written, _ = self._enrich_markets(condition_ids)
        return {"markets": written, "closed_positions": 0, "trades": 0, "snapshots": 0}

    def _collect_condition_ids(self, wallets: list[str]) -> set[str]:
        if not wallets:
            return set()
        stmt = select(Trade.condition_id).where(Trade.wallet.in_(wallets)).distinct()
        return {row[0] for row in self.session.execute(stmt).all() if row[0]}

    def _enrich_markets(self, condition_ids: set[str]) -> tuple[int, int]:
        if not condition_ids:
            return 0, 0
        rows = self.client.fetch_markets(condition_ids)
        upserts = [self._market_row(r) for r in rows]
        upserts = _dedup_payloads(upserts, "condition_id")
        if upserts:
            _upsert(
                self.session,
                Market.__table__,
                upserts,
                index_cols=[Market.__table__.c.condition_id],
                update_cols=[
                    "question", "slug", "event_slug", "category", "start_time",
                    "end_time", "volume", "liquidity", "active", "closed",
                    "token_yes", "token_no", "outcomes", "outcome_prices",
                    "raw", "updated_at",
                ],
            )
            self.session.execute(
                Market.__table__.update()
                .where(Market.__table__.c.condition_id.in_([u["condition_id"] for u in upserts]))
                .values(updated_at=sql_func.now())
            )
        return len(upserts), 0

    def _market_row(self, row: dict) -> dict:
        tokens = as_json_list(row.get("clobTokenIds")) or []
        return {
            "condition_id": _cid(row),
            "gamma_id": str(row["id"]) if row.get("id") else None,
            "question": row.get("question") or "",
            "slug": row.get("slug"),
            "event_slug": row.get("eventSlug"),
            "category": row.get("category"),
            "start_time": as_datetime(row.get("startDate") or row.get("startDateIso")),
            "end_time": as_datetime(row.get("endDate") or row.get("endDateIso")),
            "volume": as_decimal(row.get("volumeNum") or row.get("volume")),
            "liquidity": as_decimal(row.get("liquidityNum") or row.get("liquidity")),
            "active": as_bool(row.get("active")),
            "closed": as_bool(row.get("closed")),
            "token_yes": tokens[0] if len(tokens) >= 1 else None,
            "token_no": tokens[1] if len(tokens) >= 2 else None,
            "outcomes": as_json_list(row.get("outcomes")),
            "outcome_prices": as_json_list(row.get("outcomePrices")),
            "raw": row,
            "updated_at": utc_now(),
        }

    def _persist_closed_positions(self, wallets: list[str]) -> int:
        """Insert closed-position rows for each wallet (with 90d lookback).

        Note: markets enrichment is handled by the dedicated ``markets`` job
        (every 6h). Calling _persist_closed_positions from a 5min high-frequency
        path stays here so we don't double-enrich; the markets job will pick
        up the condition_ids on its next run.
        """
        lookback = utc_now() - timedelta(days=self.settings.activity_lookback_days)
        written = 0
        now = utc_now()
        logger.info("_persist_closed_positions: %d wallets, lookback=%s", len(wallets), lookback)

        for i, wallet in enumerate(wallets):
            wallet_payloads: list[dict] = []
            try:
                for row in self.client.iter_closed_positions(wallet, max_rows=self.settings.closed_positions_max_rows):
                    closed_at = as_datetime(row.get("timestamp"))
                    if closed_at is None or closed_at < lookback:
                        continue
                    cond = _cid(row)
                    token = str(row.get("asset") or "")
                    if not cond:
                        continue
                    fp = stable_fingerprint(
                        "closed", wallet, cond, token, closed_at, row.get("realizedPnl")
                    )
                    wallet_payloads.append({
                        "fingerprint": fp,
                        "wallet": wallet,
                        "condition_id": cond,
                        "token_id": token,
                        "outcome": row.get("outcome") or "",
                        "avg_price": as_decimal(row.get("avgPrice")),
                        "total_bought": as_decimal(row.get("totalBought")),
                        "realized_pnl": as_decimal(row.get("realizedPnl")),
                        "current_price": as_decimal(row.get("curPrice")),
                        "closed_at": closed_at,
                        "title": row.get("title"),
                        "slug": row.get("slug"),
                        "event_slug": row.get("eventSlug"),
                        "end_time": as_datetime(row.get("endDate")),
                        "raw": row,
                        "collected_at": now,
                    })
                logger.info("  wallet=%s raw_rows=%d", wallet, len(wallet_payloads))
            except RuntimeError as exc:
                logger.warning("Closed positions failed wallet=%s: %s", wallet, exc)
                continue

            wallet_payloads = _dedup_payloads(wallet_payloads, "fingerprint")
            if wallet_payloads:
                _upsert(
                    self.session,
                    ClosedPosition.__table__,
                    wallet_payloads,
                    index_cols=[ClosedPosition.__table__.c.fingerprint],
                    update_cols=[
                        "avg_price", "total_bought", "realized_pnl", "current_price",
                        "closed_at", "title", "slug", "event_slug", "end_time",
                        "raw", "collected_at",
                    ],
                )
                written += len(wallet_payloads)
                logger.info("  wallet=%s wrote %d closed positions", wallet, len(wallet_payloads))

        logger.info("_persist_closed_positions: total_written=%d", written)
        return written

    # ------------------------------------------------------------------
    # Activity / Trades
    # ------------------------------------------------------------------

    def collect_activity(self, wallets: list[str]) -> dict[str, int]:
        start_epoch = int(
            (utc_now() - timedelta(days=self.settings.activity_lookback_days)).timestamp()
        )
        written = 0
        markets_touched = 0
        for wallet in wallets:
            try:
                rows = self.client.fetch_activity(
                    wallet,
                    start_epoch=start_epoch,
                    activity_type="TRADE",
                    max_rows=self.settings.activity_max_rows,
                )
            except RuntimeError as exc:
                logger.warning("Activity fetch failed wallet=%s: %s", wallet, exc)
                continue
            now = utc_now()
            last_active: datetime | None = None
            payloads = []
            condition_ids: set[str] = set()
            for row in rows:
                traded_at = as_datetime(row.get("timestamp"))
                if traded_at is None:
                    continue
                cond = _cid(row)
                token = str(row.get("asset") or "")
                fp = stable_fingerprint(
                    "trade", wallet, cond, token, traded_at,
                    row.get("side"), row.get("price"), row.get("size"),
                    row.get("transactionHash"),
                )
                price = as_decimal(row.get("price"))
                size = as_decimal(row.get("size"))
                usdc = as_decimal(row.get("usdcSize"))
                payloads.append({
                    "fingerprint": fp,
                    "wallet": wallet,
                    "condition_id": cond,
                    "token_id": token or None,
                    "transaction_hash": row.get("transactionHash"),
                    "side": (row.get("side") or "BUY").upper(),
                    "outcome": row.get("outcome") or "",
                    "outcome_index": as_int(row.get("outcomeIndex")),
                    "price": price,
                    "size": size,
                    "amount": usdc if usdc else price * size,
                    "traded_at": traded_at,
                    "title": row.get("title"),
                    "slug": row.get("slug"),
                    "event_slug": row.get("eventSlug"),
                    "raw": row,
                    "collected_at": now,
                })
                if traded_at and (last_active is None or traded_at > last_active):
                    last_active = traded_at
                if cond:
                    condition_ids.add(cond)
            payloads = _dedup_payloads(payloads, "fingerprint")
            if payloads:
                _upsert(
                    self.session,
                    Trade.__table__,
                    payloads,
                    index_cols=[Trade.__table__.c.fingerprint],
                    update_cols=["raw", "collected_at"],
                )
                written += len(payloads)
            if condition_ids:
                new_m, _ = self._enrich_markets(condition_ids)
                markets_touched += new_m
            if last_active:
                self.session.execute(
                    Trader.__table__.update()
                    .where(Trader.__table__.c.wallet == wallet)
                    .values(
                        last_collected_at=now,
                        last_active_at=last_active,
                    )
                )
        # Piggy-back: closed positions share the same wallet set + lookback
        # window, so writing them here keeps the realized PnL view fresh
        # without adding API calls (we already have each wallet in hand).
        try:
            closed_count = self._persist_closed_positions(wallets)
        except Exception:
            logger.exception("closed_positions persist failed (continuing)")
            closed_count = 0
        return {"trades": written, "markets": markets_touched, "closed_positions": closed_count}

    # ------------------------------------------------------------------
    # Current positions
    # ------------------------------------------------------------------

    def collect_current_positions(self, wallets: list[str]) -> dict[str, int]:
        written = 0
        snapshots = 0
        markets_touched = 0
        for wallet in wallets:
            try:
                rows = self.client.fetch_positions(wallet, max_rows=self.settings.positions_max_rows)
            except RuntimeError as exc:
                logger.warning("Positions fetch failed wallet=%s: %s", wallet, exc)
                continue
            logger.info(
                "collect_current_positions: wallet=%s raw_rows=%d active_after_filter=%d",
                wallet, len(rows),
                sum(1 for r in rows if as_decimal(r.get("size")) > 0),
            )
            now = utc_now()
            payloads: list[dict] = []
            snap_payloads: list[dict] = []
            condition_ids: set[str] = set()
            for row in rows:
                cond = _cid(row)
                token = str(row.get("asset") or "")
                size = as_decimal(row.get("size"))
                if size <= 0:
                    continue
                if cond:
                    condition_ids.add(cond)
                cur_price = as_decimal(row.get("curPrice"))
                payloads.append({
                    "wallet": wallet,
                    "token_id": token,
                    "condition_id": cond,
                    "outcome": row.get("outcome") or "",
                    "outcome_index": as_int(row.get("outcomeIndex")),
                    "size": size,
                    "avg_price": as_decimal(row.get("avgPrice")),
                    "current_price": cur_price,
                    "initial_value": as_decimal(row.get("initialValue")),
                    "current_value": as_decimal(row.get("currentValue")),
                    "cash_pnl": as_decimal(row.get("cashPnl")),
                    "realized_pnl": as_decimal(row.get("realizedPnl")),
                    "percent_pnl": as_decimal(row.get("percentPnl")),
                    "total_bought": as_decimal(row.get("totalBought")),
                    "title": row.get("title"),
                    "slug": row.get("slug"),
                    "event_slug": row.get("eventSlug"),
                    "end_time": as_datetime(row.get("endDate")),
                    "first_observed_at": now,
                    "observed_at": now,
                    "raw": row,
                })
                snap_payloads.append({
                    "fingerprint": stable_fingerprint("snap", wallet, cond, token, now),
                    "observed_at": now,
                    "wallet": wallet,
                    "condition_id": cond,
                    "token_id": token,
                    "outcome": row.get("outcome") or "",
                    "size": size,
                    "avg_price": as_decimal(row.get("avgPrice")),
                    "current_price": cur_price,
                    "current_value": as_decimal(row.get("currentValue")),
                    "cash_pnl": as_decimal(row.get("cashPnl")),
                })
            self.session.execute(
                delete(CurrentPosition.__table__)
                .where(CurrentPosition.__table__.c.wallet == wallet)
            )
            payloads = _dedup_payloads(payloads, "token_id")
            if payloads:
                _upsert(
                    self.session,
                    CurrentPosition.__table__,
                    payloads,
                    index_cols=[
                        CurrentPosition.__table__.c.wallet,
                        CurrentPosition.__table__.c.token_id,
                    ],
                    update_cols=[
                        "outcome", "outcome_index", "size", "avg_price",
                        "current_price", "initial_value", "current_value",
                        "cash_pnl", "realized_pnl", "percent_pnl",
                        "total_bought", "title", "slug", "event_slug",
                        "end_time", "observed_at", "raw",
                    ],
                )
                written += len(payloads)
            snap_payloads = _dedup_payloads(snap_payloads, "fingerprint")
            if snap_payloads:
                self.session.execute(pg_insert(PositionSnapshot.__table__).values(snap_payloads))
                snapshots += len(snap_payloads)
            if condition_ids:
                new_m, _ = self._enrich_markets(condition_ids)
                markets_touched += new_m
        return {"positions": written, "snapshots": snapshots, "markets": markets_touched}

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def _prune_stale_trackers(self) -> None:
        cutoff = utc_now() - timedelta(days=self.settings.activity_lookback_days * 2)
        self.session.execute(
            Trader.__table__.update()
            .where(Trader.__table__.c.last_active_at < cutoff)
            .values(tracked=False)
        )
