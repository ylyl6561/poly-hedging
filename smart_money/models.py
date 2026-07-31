from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

MONEY = Numeric(24, 8)
PRICE = Numeric(18, 10)


class Base(DeclarativeBase):
    pass


class Trader(Base):
    __tablename__ = "smart_money_traders"

    wallet: Mapped[str] = mapped_column(String(42), primary_key=True)
    username: Mapped[str | None] = mapped_column(String(255))
    pseudonym: Mapped[str | None] = mapped_column(String(255))
    profile_image: Mapped[str | None] = mapped_column(Text)
    x_username: Mapped[str | None] = mapped_column(String(255))
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    tracked: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LeaderboardEntry(Base):
    __tablename__ = "smart_money_leaderboard_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    time_period: Mapped[str] = mapped_column(String(16), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    wallet: Mapped[str] = mapped_column(
        ForeignKey("smart_money_traders.wallet", ondelete="CASCADE"), nullable=False
    )
    pnl: Mapped[Decimal] = mapped_column(MONEY, default=0, nullable=False)
    volume: Mapped[Decimal] = mapped_column(MONEY, default=0, nullable=False)
    raw: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "collected_at", "category", "time_period", "wallet",
            name="uq_smart_money_leaderboard_snapshot_wallet",
        ),
        Index(
            "ix_smart_money_leaderboard_latest",
            "category", "time_period", "collected_at", "rank",
        ),
    )


class Market(Base):
    __tablename__ = "smart_money_markets"

    condition_id: Mapped[str] = mapped_column(String(66), primary_key=True)
    gamma_id: Mapped[str | None] = mapped_column(String(64))
    question: Mapped[str] = mapped_column(Text, default="", nullable=False)
    slug: Mapped[str | None] = mapped_column(String(512))
    event_slug: Mapped[str | None] = mapped_column(String(512))
    category: Mapped[str | None] = mapped_column(String(128))
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    volume: Mapped[Decimal | None] = mapped_column(MONEY)
    liquidity: Mapped[Decimal | None] = mapped_column(MONEY)
    active: Mapped[bool | None] = mapped_column(Boolean)
    closed: Mapped[bool | None] = mapped_column(Boolean)
    token_yes: Mapped[str | None] = mapped_column(String(128))
    token_no: Mapped[str | None] = mapped_column(String(128))
    outcomes: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    outcome_prices: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    raw: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_smart_money_markets_end_time", "end_time"),
        Index("ix_smart_money_markets_category", "category"),
    )


class Trade(Base):
    __tablename__ = "smart_money_trades"

    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    wallet: Mapped[str] = mapped_column(
        ForeignKey("smart_money_traders.wallet", ondelete="CASCADE"), nullable=False
    )
    condition_id: Mapped[str] = mapped_column(
        ForeignKey("smart_money_markets.condition_id", ondelete="CASCADE", initially="DEFERRED"), nullable=False
    )
    token_id: Mapped[str | None] = mapped_column(String(128))
    transaction_hash: Mapped[str | None] = mapped_column(String(128))
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    outcome: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    outcome_index: Mapped[int | None] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    size: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    traded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    slug: Mapped[str | None] = mapped_column(String(512))
    event_slug: Mapped[str | None] = mapped_column(String(512))
    raw: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_smart_money_trades_wallet_time", "wallet", "traded_at"),
        Index("ix_smart_money_trades_market_time", "condition_id", "traded_at"),
        Index("ix_smart_money_trades_recent", "traded_at", "side"),
    )


class CurrentPosition(Base):
    __tablename__ = "smart_money_current_positions"

    wallet: Mapped[str] = mapped_column(
        ForeignKey("smart_money_traders.wallet", ondelete="CASCADE"), primary_key=True
    )
    token_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    condition_id: Mapped[str] = mapped_column(
        ForeignKey("smart_money_markets.condition_id", ondelete="CASCADE", initially="DEFERRED"), nullable=False
    )
    outcome: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    outcome_index: Mapped[int | None] = mapped_column(Integer)
    size: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    avg_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    current_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    initial_value: Mapped[Decimal] = mapped_column(MONEY, default=0, nullable=False)
    current_value: Mapped[Decimal] = mapped_column(MONEY, default=0, nullable=False)
    cash_pnl: Mapped[Decimal] = mapped_column(MONEY, default=0, nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(MONEY, default=0, nullable=False)
    percent_pnl: Mapped[Decimal] = mapped_column(PRICE, default=0, nullable=False)
    total_bought: Mapped[Decimal] = mapped_column(MONEY, default=0, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    slug: Mapped[str | None] = mapped_column(String(512))
    event_slug: Mapped[str | None] = mapped_column(String(512))
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    __table_args__ = (
        Index("ix_smart_money_positions_market", "condition_id", "outcome"),
        Index("ix_smart_money_positions_observed", "observed_at"),
    )


class PositionSnapshot(Base):
    __tablename__ = "smart_money_position_snapshots"

    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    wallet: Mapped[str] = mapped_column(
        ForeignKey("smart_money_traders.wallet", ondelete="CASCADE"), nullable=False
    )
    condition_id: Mapped[str] = mapped_column(
        ForeignKey("smart_money_markets.condition_id", ondelete="CASCADE", initially="DEFERRED"), nullable=False
    )
    token_id: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    size: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    avg_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    current_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    current_value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    cash_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

    __table_args__ = (
        Index("ix_smart_money_position_history", "wallet", "observed_at"),
    )


class ClosedPosition(Base):
    __tablename__ = "smart_money_closed_positions"

    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    wallet: Mapped[str] = mapped_column(
        ForeignKey("smart_money_traders.wallet", ondelete="CASCADE"), nullable=False
    )
    condition_id: Mapped[str] = mapped_column(
        ForeignKey("smart_money_markets.condition_id", ondelete="CASCADE", initially="DEFERRED"), nullable=False
    )
    token_id: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    avg_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    total_bought: Mapped[Decimal] = mapped_column(MONEY, default=0, nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(MONEY, default=0, nullable=False)
    current_price: Mapped[Decimal] = mapped_column(PRICE, default=0, nullable=False)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    slug: Mapped[str | None] = mapped_column(String(512))
    event_slug: Mapped[str | None] = mapped_column(String(512))
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_smart_money_closed_wallet_time", "wallet", "closed_at"),
        Index("ix_smart_money_closed_pnl_time", "closed_at", "realized_pnl"),
    )


class CollectionRun(Base):
    __tablename__ = "smart_money_collection_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rows_seen: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_written: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    __table_args__ = (Index("ix_smart_money_runs_job_time", "job_name", "started_at"),)


class TrackedWalletSnapshot(Base):
    """Latest wallet list chosen by the trades job.

    Populated on every trades tick, read by the positions tick to ensure both
    jobs operate on the *same* wallet set within a cycle. Avoids the
    ~10-wallet drift caused by independent ``ORDER BY`` tie-breaking.
    """

    __tablename__ = "smart_money_tracked_wallets_snapshot"

    job_name: Mapped[str] = mapped_column(String(32), primary_key=True)
    wallet: Mapped[str] = mapped_column(String(42), primary_key=True)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_snapshot_recorded_at", "job_name", "recorded_at"),)


class TraderScore(Base):
    """Computed trader profile (lifetime window). Updated on every scoring run.

    Captures the lifetime trader image: all-time win rate, ROI, average
    holding time, side bias (YES vs NO), category bias, and an overall
    ``smart_money_score`` in [0, 100] used for ranking.

    For the **short-window** view (last 30 days by default) see
    :class:`WindowScore` — that is what the user-facing "跟单名单"
    filtering keys off.
    """

    __tablename__ = "smart_money_trader_scores"

    wallet: Mapped[str] = mapped_column(String(42), primary_key=True)
    username: Mapped[str | None] = mapped_column(String(255))
    pseudonym: Mapped[str | None] = mapped_column(String(255))
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Volume / activity
    trade_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    closed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    open_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_volume: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # PnL / ROI
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    total_pnl: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    roi_pct: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # Win rate = closed positions with realized_pnl > 0 / closed_count
    win_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    avg_win: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    avg_loss: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    avg_hold_hours: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    max_drawdown: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # Bias features
    yes_ratio: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    top_category: Mapped[str | None] = mapped_column(String(64))

    # Composite score (0..100), rankable
    smart_money_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    rank: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_trader_scores_rank", "smart_money_score"),
        Index("ix_trader_scores_total_pnl", "total_pnl"),
    )


class WindowScore(Base):
    """Short-window trader profile — the table the dashboard's
    "跟单名单 / Trader Scores" panel actually ranks.

    Where :class:`TraderScore` is *lifetime* (all-time stats), this
    table recomputes the same picture but **limited to the last
    ``window_days``** — default 30.  Concretely:

    * ``trade_count`` / ``closed_count`` / ``open_count`` — activity
      inside the window only.
    * ``total_volume`` — sum of |amount| traded inside the window.
    * ``realized_pnl`` — closed positions closed inside the window.
    * ``unrealized_pnl`` — currently open positions' cash PnL
      (always "current"; cached only for convenience).
    * ``total_pnl`` — sum of the two above.
    * ``roi_pct`` — ``realized_pnl / total_volume * 100``.
    * ``win_rate`` — wins / closed.
    * ``days_active`` — number of distinct days the wallet traded.
    * ``avg_daily_pnl`` — ``total_pnl / max(days_active, 1)``.
    * ``last_active_at`` — most recent Trade.traded_at in window.
    * ``days_since_active`` — now - last_active_at in days.
    * ``open_position_count`` — number of ``CurrentPosition`` rows
      (NOT window-bounded — current = always current).
    * ``smart_window_score`` — composite [0, 100] keyed off window
      PnL / ROI / days_active / recency.  This is what the
      "Trader Scores · 智能资金画像" panel sorts by.

    The user requirement: *"最近 1 个月盈利最高、ROI 最高、且每天有
    有效持仓的账号"* — i.e. wallet must have

    * a non-trivial positive ``realized_pnl`` inside 30 days,
    * a high window ``roi_pct``,
    * multiple active days (``days_active >= 5``),
    * positions currently live (``open_position_count >= 1``).

    These thresholds are tunable via SmartMoneySettings + the
    followlist filter.
    """

    __tablename__ = "smart_money_window_scores"

    wallet: Mapped[str] = mapped_column(String(42), primary_key=True)
    username: Mapped[str | None] = mapped_column(String(255))
    pseudonym: Mapped[str | None] = mapped_column(String(255))
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Window length for these rows (so the dashboard knows if the
    # numbers are 7d vs 30d vs 90d).
    window_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)

    # Activity
    trade_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    closed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    days_active: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    days_since_active: Mapped[float] = mapped_column(Float, default=999.0, nullable=False)

    # Money
    total_volume: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    total_pnl: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    roi_pct: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    win_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    avg_daily_pnl: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # --- Profit-stability metrics (Phase 5, user-driven) ---
    # These columns exist to *filter* out wallets that spike big but
    # draw down hard, or that are otherwise not the "stable, low-drawdown
    # hidden winners" the user wants to copy.  Values are computed
    # entirely from per-day net PnL aggregation inside the window —
    # see :mod:`smart_money.window_scoring`.

    # How many distinct days inside the window had realised PnL > 0.
    profit_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # How many distinct days inside the window had realised PnL < 0.
    # The follow-list filter requires this to be 0 (i.e. *no losing
    # days* inside the window — a "稳定盈利" requirement).
    loss_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Longest consecutive run of profit-days inside the window.
    longest_win_streak: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Max drawdown of the cumulative realised-PnL curve, expressed as
    # a *percentage of peak*.  0% = monotonically up; 50% = half of
    # peak-to-trough from the running high.
    max_drawdown_pct: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # Std-dev of per-day realised PnL (USDC).  High stddev = "wildly
    # variable day-by-day returns".  Used as an inverse stability
    # signal in the composite score.
    daily_pnl_stddev: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # Earliest Trade.traded_at inside the window.  Combined with
    # ``last_active_at`` this gives the *period span* — i.e. how
    # long the wallet has been active inside the window.  The
    # follow-list filter requires this to be close to the full
    # window length (i.e. not a single-week burst).
    earliest_trade_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Number of days between ``earliest_trade_at`` and now.  Same as
    # ``period_days`` in the rest of the codebase — stored here as a
    # column so the follow filter can do an index-friendly comparison.
    period_days: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # Current state — NOT window-bounded (always present-tense).
    open_position_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Categorical
    top_category: Mapped[str | None] = mapped_column(String(64))

    # Composite
    smart_window_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    rank: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_window_scores_rank", "smart_window_score"),
        Index("ix_window_scores_window_days", "window_days"),
        Index("ix_window_scores_total_pnl", "total_pnl"),
        Index("ix_window_scores_days_active", "days_active"),
    )


class Signal(Base):
    """A discrete smart-money event the user can act on.

    Two flavours:
      * ``new_open`` — one tracked trader enters a condition we haven't seen
        this wallet in before within the lookback window.
      * ``consensus`` — multiple tracked traders converge on the same
        outcome for one condition within the recent window.
    """

    __tablename__ = "smart_money_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_type: Mapped[str] = mapped_column(String(32), nullable=False)
    condition_id: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome_index: Mapped[int | None] = mapped_column(Integer)
    outcome_label: Mapped[str | None] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(8), nullable=False)  # YES / NO

    # Triggering context
    trigger_wallets: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    trader_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    trigger_trade_fingerprint: Mapped[str | None] = mapped_column(String(128))
    title: Mapped[str | None] = mapped_column(Text)
    slug: Mapped[str | None] = mapped_column(String(255))
    category: Mapped[str | None] = mapped_column(String(64))
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Aggregated metrics
    total_value: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    avg_entry_price: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    current_price: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # Risk state — populated by risk filter
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    risk_reasons: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    suggested_size_usdc: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_signals_status", "status", "created_at"),
        Index("ix_signals_condition", "condition_id", "signal_type", "created_at"),
    )


class RiskFilterRun(Base):
    """Per-signal risk filter decision log — useful for audit + tuning."""

    __tablename__ = "smart_money_risk_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)  # pass / block / shrink
    reasons: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    suggested_size_usdc: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_risk_runs_signal", "signal_id", "created_at"),)


class FollowListEntry(Base):
    """A wallet approved for copy-trading.

    Recomputed on every trades tick from ``smart_money_trader_scores`` using
    the thresholds in ``SmartMoneySettings``. The list is what the
    follow-loop / executor reads.

    ``source`` tells the dashboard whether a wallet made the list via
    the *automatic* stability + recency filter ("auto") or via the
    *manual* admin override ("manual").  Manual entries are added by
    the user via :class:`ManualFollow` and bypass the auto filter —
    they still must be active (recency), but the PnL/drawdown checks
    are relaxed.
    """

    __tablename__ = "smart_money_follow_list"

    wallet: Mapped[str] = mapped_column(String(42), primary_key=True)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    username: Mapped[str | None] = mapped_column(String(255))
    pseudonym: Mapped[str | None] = mapped_column(String(255))
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    smart_money_score: Mapped[float] = mapped_column(Float, nullable=False)
    win_rate: Mapped[float] = mapped_column(Float, nullable=False)
    roi_pct: Mapped[float] = mapped_column(Float, nullable=False)
    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False)
    closed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    top_category: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(16), default="auto", nullable=False)
    note: Mapped[str | None] = mapped_column(Text)  # e.g. manual reason
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_follow_list_rank", "rank"),)


class ManualFollow(Base):
    """User-curated "follow" overrides.

    A wallet in this table is *guaranteed* to appear in the
    :class:`FollowListEntry` table on the next refresh, even if the
    automatic stability/recency filter would otherwise reject it.  This
    lets the operator pin a wallet they've hand-vetted — e.g. a known
    insider or a quiet consistent winner who doesn't show up on the
    public leaderboard yet.

    The auto filter still applies the *recency* rule (we refuse to
    follow a wallet that hasn't traded in days) and the *trade_count*
    floor — that's because if a wallet is dead we obviously can't
    copy-trade it.  Everything else (PnL / drawdown / loss_days) is
    relaxed for ``manual`` entries; the operator's job is to have
    already verified those.
    """

    __tablename__ = "smart_money_manual_follow"

    wallet: Mapped[str] = mapped_column(String(42), primary_key=True)
    username: Mapped[str | None] = mapped_column(String(255))
    note: Mapped[str | None] = mapped_column(Text)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_manual_follow_added_at", "added_at"),)


class FollowOrder(Base):
    """Audit log for every order the executor handles (real or dry-run)."""

    __tablename__ = "smart_money_follow_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(Integer, nullable=False)
    wallet: Mapped[str] = mapped_column(String(42), nullable=False)
    condition_id: Mapped[str] = mapped_column(String(128), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    token_id: Mapped[str | None] = mapped_column(String(128))
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    size_usdc: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_follow_orders_signal", "signal_id"),
        Index("ix_follow_orders_status", "status", "created_at"),
    )


class OrderEventLog(Base):
    """Persisted copy of every :class:`api.OrderEvent` the executor publishes.

    The :class:`api.OrderEventBus` is in-process, so the worker (separate
    process) cannot push events to the dashboard's SSE stream directly.
    Instead, ``OrderEventBus.publish`` writes a row here so the
    dashboard's ``/api/order-events/recent`` endpoint can read events
    across the worker → serve boundary.

    Rows are append-only.  We keep at most ~7 days (the collector's
    follow tick trims older rows to keep the table small).
    """

    __tablename__ = "smart_money_order_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    leader_wallet: Mapped[str] = mapped_column(String(42), nullable=False, default="")
    market_id: Mapped[str | None] = mapped_column(String(128))
    asset_id: Mapped[str | None] = mapped_column(String(128))
    side: Mapped[str | None] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    ts: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # The dashboard polls ``since_id=N`` to fetch only new rows.
        Index("ix_order_events_order", "order_id"),
        Index("ix_order_events_status", "status", "created_at"),
        Index("ix_order_events_created", "created_at"),
    )
