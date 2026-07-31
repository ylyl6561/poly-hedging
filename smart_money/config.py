from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from core.config import load_env_file

load_env_file(__file__)


@dataclass(frozen=True)
class SmartMoneySettings:
    database_url: str = "postgresql+psycopg://yuliang:123456@localhost:5432/polymarket"
    data_api_base: str = "https://data-api.polymarket.com"
    gamma_api_base: str = "https://gamma-api.polymarket.com"
    request_timeout_seconds: float = 15.0
    request_max_retries: int = 4
    # Cloudflare fronting the Data API starts rejecting IPs that exceed a
    # sustained ~5 req/s per source.  We default to 2 req/s here (0.5s/req)
    # which keeps a full leaderboard+positions tick comfortably under that
    # ceiling.  Override lower only if you have a proxy pool.
    request_min_interval_seconds: float = 0.5
    # Leaderboard updates at most a few times an hour.  Cache responses
    # for an hour by default to spare the Data API.
    leaderboard_cache_ttl_seconds: float = 3600.0
    top_trader_limit: int = 200
    tracked_wallet_limit: int = 100
    activity_lookback_days: int = 90
    activity_max_rows: int = 500
    positions_max_rows: int = 2000
    closed_positions_max_rows: int = 200
    recent_trade_hours: int = 24
    min_consensus_traders: int = 2
    dashboard_refresh_seconds: int = 60
    dashboard_dir: Path = Path(__file__).resolve().parent / "dashboard"

    # ---- Signal / scoring / risk knobs ----
    signal_recent_hours: int = 6
    signal_min_trader_score: float = 30.0
    min_signal_confidence: float = 0.4
    # Production: set to ~5 to require a signal to age 5min before it can pass.
    # Default 0 = disabled (single tick scenario).
    min_signal_age_minutes: int = 0
    require_consensus_for_new_open: bool = False

    # Risk
    market_end_min_hours: float = 1.0
    market_end_max_days: float = 365.0
    min_signal_price: float = 0.05
    max_signal_price: float = 0.95
    min_volume_24h_usdc: float = 500.0
    block_illiquid: bool = True
    default_position_size_usdc: float = 50.0
    max_position_size_usdc: float = 250.0

    # ---- Follow list (copy-trading whitelist) ----
    # Trader must satisfy all thresholds to be on the follow list.
    # Phase-5 (stability-focused) defaults — "稳定盈利 + 低回撤 + 仍
    # 在交易".  Manual entries (added via /api/follow-manual/*) bypass
    # the PnL / loss_days / drawdown checks but still must be active.
    follow_min_window_score: float = 40.0
    follow_min_days_active: int = 5
    follow_min_open_positions: int = 1
    follow_min_window_pnl: float = 100.0
    follow_min_trade_count: int = 3
    follow_max_idle_days: int = 3
    # Phase 5 — new gates:
    follow_max_trade_count: int = 30      # 高频炒单拒
    follow_max_loss_days: int = 0         # 30 天内 0 亏损日 = 硬门槛
    follow_max_drawdown_pct: float = 30.0 # 最大回撤 ≤ 30%
    follow_min_period_days: float = 14.0  # 最早一笔 ≤ 30 天窗口里的前 14 天
    follow_list_max: int = 100
    follow_top_n_for_signals: int = 5

    # ---- Follow executor ----
    # Only consensus signals with this many traders are eligible for auto-execute.
    follow_min_consensus_for_execute: int = 3
    # Auto-execute enabled flag (set SMART_MONEY_LIVE_TRADE=1 to enable real orders).
    live_trade: bool = False
    # Confirmation window before auto-cancel (seconds).
    follow_confirm_timeout_seconds: int = 30
    # Single-signal max USDC to deploy (overrides risk-suggested size if smaller).
    follow_max_size_usdc: float = 100.0
    # CLOB order type: FOK / GTC / FAK. FAK cancels remainder (safer).
    follow_clob_order_type:  str = "FAK"
    # Live-trade position caps.  Defaults are deliberately tiny (first-time
    # wiring) so that a misfire costs cents, not dollars.
    live_min_size_usdc: float = 1.0
    live_default_size_usdc: float = 1.0
    live_max_daily_usdc: float = 5.0
    live_max_concurrent_per_condition: int = 2
    # When True, a ``live_trade=True`` executor will write the order audit
    # row with status='pending' and skip the actual CLOB call.  A human
    # then approves via the /orders/{id}/approve dashboard endpoint.
    # Default is True so a flipped live_trade toggle never auto-fires
    # a real order without explicit per-order approval.
    semi_auto: bool = True
    # Polymarket CLOB credentials (overrides SIMMER_*_WALLET_* via the same
    # underlying values when those are not set).  ``funder`` is the EOA or
    # proxy address that holds USDC; ``private_key`` is the L1 signer.
    polymarket_private_key: str = ""
    polymarket_funder: str = ""
    polymarket_signature_type: int = 2
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_api_passphrase: str = ""

    # ---- Feishu notifier ----
    feishu_webhook_url: str = ""


@lru_cache(maxsize=1)
def get_settings() -> SmartMoneySettings:
    return SmartMoneySettings(
        database_url=os.environ.get(
            "SMART_MONEY_DATABASE_URL",
            SmartMoneySettings.database_url,
        ),
        data_api_base=os.environ.get(
            "SMART_MONEY_DATA_API_BASE",
            SmartMoneySettings.data_api_base,
        ).rstrip("/"),
        gamma_api_base=os.environ.get(
            "SMART_MONEY_GAMMA_API_BASE",
            SmartMoneySettings.gamma_api_base,
        ).rstrip("/"),
        request_timeout_seconds=float(
            os.environ.get("SMART_MONEY_REQUEST_TIMEOUT_SECONDS", "15")
        ),
        request_max_retries=int(
            os.environ.get("SMART_MONEY_REQUEST_MAX_RETRIES", "4")
        ),
        request_min_interval_seconds=float(
            os.environ.get("SMART_MONEY_REQUEST_MIN_INTERVAL_SECONDS", "0.5")
        ),
        leaderboard_cache_ttl_seconds=float(
            os.environ.get("SMART_MONEY_LEADERBOARD_CACHE_TTL_SECONDS", "3600")
        ),
        top_trader_limit=int(os.environ.get("SMART_MONEY_TOP_TRADER_LIMIT", "200")),
        tracked_wallet_limit=int(
            os.environ.get("SMART_MONEY_TRACKED_WALLET_LIMIT", "100")
        ),
        activity_lookback_days=int(
            os.environ.get("SMART_MONEY_ACTIVITY_LOOKBACK_DAYS", "90")
        ),
        activity_max_rows=int(
            os.environ.get("SMART_MONEY_ACTIVITY_MAX_ROWS", "500")
        ),
        positions_max_rows=int(
            os.environ.get("SMART_MONEY_POSITIONS_MAX_ROWS", "2000")
        ),
        closed_positions_max_rows=int(
            os.environ.get("SMART_MONEY_CLOSED_POSITIONS_MAX_ROWS", "200")
        ),
        recent_trade_hours=int(
            os.environ.get("SMART_MONEY_RECENT_TRADE_HOURS", "24")
        ),
        min_consensus_traders=int(
            os.environ.get("SMART_MONEY_MIN_CONSENSUS_TRADERS", "2")
        ),
        dashboard_refresh_seconds=int(
            os.environ.get("SMART_MONEY_DASHBOARD_REFRESH_SECONDS", "60")
        ),
        signal_recent_hours=int(
            os.environ.get("SMART_MONEY_SIGNAL_RECENT_HOURS", "6")
        ),
        signal_min_trader_score=float(
            os.environ.get("SMART_MONEY_SIGNAL_MIN_TRADER_SCORE", "30")
        ),
        min_signal_confidence=float(
            os.environ.get("SMART_MONEY_MIN_SIGNAL_CONFIDENCE", "0.4")
        ),
        min_signal_age_minutes=int(
            os.environ.get("SMART_MONEY_MIN_SIGNAL_AGE_MINUTES", "0")
        ),
        require_consensus_for_new_open=(
            os.environ.get("SMART_MONEY_REQUIRE_CONSENSUS_FOR_NEW_OPEN", "0") == "1"
        ),
        market_end_min_hours=float(
            os.environ.get("SMART_MONEY_MARKET_END_MIN_HOURS", "1")
        ),
        market_end_max_days=float(
            os.environ.get("SMART_MONEY_MARKET_END_MAX_DAYS", "365")
        ),
        min_signal_price=float(
            os.environ.get("SMART_MONEY_MIN_SIGNAL_PRICE", "0.05")
        ),
        max_signal_price=float(
            os.environ.get("SMART_MONEY_MAX_SIGNAL_PRICE", "0.95")
        ),
        min_volume_24h_usdc=float(
            os.environ.get("SMART_MONEY_MIN_VOLUME_24H_USDC", "500")
        ),
        block_illiquid=(
            os.environ.get("SMART_MONEY_BLOCK_ILLIQUID", "1") == "1"
        ),
        default_position_size_usdc=float(
            os.environ.get("SMART_MONEY_DEFAULT_POSITION_SIZE_USDC", "50")
        ),
        max_position_size_usdc=float(
            os.environ.get("SMART_MONEY_MAX_POSITION_SIZE_USDC", "250")
        ),
        follow_list_max=int(
            os.environ.get("SMART_MONEY_FOLLOW_LIST_MAX", "100")
        ),
        follow_top_n_for_signals=int(
            os.environ.get("SMART_MONEY_FOLLOW_TOP_N_FOR_SIGNALS", "5")
        ),
        follow_min_window_score=float(
            os.environ.get("SMART_MONEY_FOLLOW_MIN_WINDOW_SCORE", "40.0")
        ),
        follow_min_days_active=int(
            os.environ.get("SMART_MONEY_FOLLOW_MIN_DAYS_ACTIVE", "5")
        ),
        follow_min_window_pnl=float(
            os.environ.get("SMART_MONEY_FOLLOW_MIN_WINDOW_PNL", "100.0")
        ),
        follow_min_trade_count=int(
            os.environ.get("SMART_MONEY_FOLLOW_MIN_TRADE_COUNT", "3")
        ),
        follow_max_idle_days=int(
            os.environ.get("SMART_MONEY_FOLLOW_MAX_IDLE_DAYS", "3")
        ),
        follow_min_open_positions=int(
            os.environ.get("SMART_MONEY_FOLLOW_MIN_OPEN_POSITIONS", "1")
        ),
        follow_max_trade_count=int(
            os.environ.get("SMART_MONEY_FOLLOW_MAX_TRADE_COUNT", "30")
        ),
        follow_max_loss_days=int(
            os.environ.get("SMART_MONEY_FOLLOW_MAX_LOSS_DAYS", "0")
        ),
        follow_max_drawdown_pct=float(
            os.environ.get("SMART_MONEY_FOLLOW_MAX_DRAWDOWN_PCT", "30.0")
        ),
        follow_min_period_days=float(
            os.environ.get("SMART_MONEY_FOLLOW_MIN_PERIOD_DAYS", "14.0")
        ),
        follow_min_consensus_for_execute=int(
            os.environ.get("SMART_MONEY_FOLLOW_MIN_CONSENSUS_FOR_EXECUTE", "3")
        ),
        live_trade=(
            os.environ.get("SMART_MONEY_LIVE_TRADE", "0") == "1"
        ),
        follow_confirm_timeout_seconds=int(
            os.environ.get("SMART_MONEY_FOLLOW_CONFIRM_TIMEOUT_SECONDS", "30")
        ),
        follow_max_size_usdc=float(
            os.environ.get("SMART_MONEY_FOLLOW_MAX_SIZE_USDC", "100")
        ),
        follow_clob_order_type=os.environ.get(
            "SMART_MONEY_FOLLOW_CLOB_ORDER_TYPE", "FAK"
        ),
        feishu_webhook_url=os.environ.get(
            "SMART_MONEY_FEISHU_WEBHOOK_URL", ""
        ),
        live_min_size_usdc=float(
            os.environ.get("SMART_MONEY_LIVE_MIN_SIZE_USDC", "1.0")
        ),
        live_default_size_usdc=float(
            os.environ.get("SMART_MONEY_LIVE_DEFAULT_SIZE_USDC", "1.0")
        ),
        live_max_daily_usdc=float(
            os.environ.get("SMART_MONEY_LIVE_MAX_DAILY_USDC", "5.0")
        ),
        live_max_concurrent_per_condition=int(
            os.environ.get("SMART_MONEY_LIVE_MAX_CONCURRENT_PER_CONDITION", "2")
        ),
        semi_auto=(
            os.environ.get("SMART_MONEY_SEMI_AUTO", "1").lower()
            not in ("0", "false", "no")
        ),
        polymarket_private_key=os.environ.get(
            "SMART_MONEY_POLYMARKET_PRIVATE_KEY", ""
        ),
        polymarket_funder=os.environ.get(
            "SMART_MONEY_POLYMARKET_FUNDER", ""
        ),
        polymarket_signature_type=int(
            os.environ.get("SMART_MONEY_POLYMARKET_SIGNATURE_TYPE", "2")
        ),
        polymarket_api_key=os.environ.get(
            "SMART_MONEY_POLYMARKET_API_KEY", ""
        ),
        polymarket_api_secret=os.environ.get(
            "SMART_MONEY_POLYMARKET_API_SECRET", ""
        ),
        polymarket_api_passphrase=os.environ.get(
            "SMART_MONEY_POLYMARKET_API_PASSPHRASE", ""
        ),
    )
