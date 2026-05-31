#!/usr/bin/env python3
"""
Simmer FastLoop Trading Skill — CLI entry point.

Trades Polymarket BTC 5-minute fast markets using CEX price momentum.
Default signal: Binance BTCUSDT candles.

Usage:
    python fastloop_trader.py              # Dry run (show opportunities, no trades)
    python fastloop_trader.py --live       # Execute real trades
    python fastloop_trader.py --positions  # Show current fast market positions
    python fastloop_trader.py --quiet      # Only output on trades/errors
    python fastloop_trader.py --set key=value  # Update config

Requires:
    SIMMER_API_KEY environment variable (get from simmer.markets/dashboard)
"""

import os
import sys
from pathlib import Path

# Add project root to path so all modules can be imported
_project_root = Path(__file__).parent.parent.resolve()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

# Force line-buffered stdout for non-TTY environments (cron, Docker, OpenClaw)
sys.stdout.reconfigure(line_buffering=True)

# Load .env before anything else
from core import load_env_file
load_env_file(__file__)

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path

from core import CONFIG_SCHEMA, update_config, get_config_path, resolve_config, WINDOW_SECONDS, load_env_file
from state import StructuredRunLog
from state.reconcile_export import export_preopen_action_to_excel
from api import ensure_wallet_linked_with_retry, get_client
from trading import get_portfolio
from notifications import is_configured as feishu_configured, send_asset_snapshot_notification_sync
from market import discover_fast_market_markets, find_best_fast_market, start_rtds_stream
from api import lookup_fee_rate, fetch_market_outcome

# Import preopen modules
from main.preopen_runner import build_event_pool, format_event_summary
from main.preopen_event_pool import PreopenEventPool
from main.preopen_executor import execute_event_cycle
from main.preopen_arb import evaluate_arb_edge


def setup_run_logging(is_live: bool):
    """Create run folder with timestamp and setup log file redirection."""
    project_root = Path(__file__).parent
    runs_dir = project_root / "runs"
    runs_dir.mkdir(exist_ok=True)

    mode_suffix = "live" if is_live else "dryrun"
    timestamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    run_folder = runs_dir / f"{timestamp}_{mode_suffix}"
    run_folder.mkdir(exist_ok=True)

    log_file = run_folder / "output.log"
    structured_log = StructuredRunLog(
        run_folder,
        mode="live" if is_live else "dryrun",
        log_file_name=log_file.name,
    )

    class TimestampLineTee:
        """Mirror output to console and file, prefixing each completed file line once."""

        def __init__(self, original_stream, log_file, structured_log=None, on_trade=None):
            self.original_stream = original_stream
            self.log_file = open(log_file, "a", buffering=1, encoding="utf-8")
            self.structured_log = structured_log
            self._line_buffer = ""
            self.encoding = getattr(original_stream, "encoding", "utf-8")
            self._on_trade = on_trade
            self._last_trade_count = 0

        def write(self, text):
            if not text:
                return 0
            self.original_stream.write(text)
            self._line_buffer += text
            while "\n" in self._line_buffer:
                line, self._line_buffer = self._line_buffer.split("\n", 1)
                self._write_log_line(line)
            return len(text)

        def _write_log_line(self, line):
            timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            self.log_file.write(f"[{timestamp_str}] {line}\n")
            if self.structured_log:
                self.structured_log.record_line(timestamp_str, line)
                trades_now = len(self.structured_log.data.get("trades", []))
                if trades_now > self._last_trade_count:
                    self._last_trade_count = trades_now
                    if self._on_trade:
                        try:
                            self._on_trade(self.structured_log)
                        except Exception:
                            # Never block trading on export errors
                            pass

        def flush(self):
            self.original_stream.flush()
            if self._line_buffer:
                self._write_log_line(self._line_buffer)
                self._line_buffer = ""
            self.log_file.flush()
            if self.structured_log:
                self.structured_log.flush()

        def isatty(self):
            return self.original_stream.isatty()

    def _on_trade(structured_log: StructuredRunLog):
        # Reserved for future hooks; keep trade loop independent.
        return

    sys.stdout = TimestampLineTee(sys.stdout, log_file, structured_log, on_trade=_on_trade)
    sys.stderr = TimestampLineTee(sys.stderr, log_file, structured_log, on_trade=_on_trade)

    print(f"📁 日志目录: {run_folder}")
    print(f"📄 日志文件: {log_file}")
    print("=" * 60)

    return run_folder, structured_log


def main():
    parser = argparse.ArgumentParser(description="Simmer FastLoop Trading Skill")
    parser.add_argument("--live", action="store_true",
                        help="Execute real trades (default is dry-run)")
    parser.add_argument(
        "--max-events",
        type=int,
        default=None,
        help="In live mode, stop after trading this many BTC 5m events",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="(Default) Show opportunities without trading")
    parser.add_argument("--positions", action="store_true",
                        help="Show current fast market positions")
    parser.add_argument("--config", action="store_true",
                        help="Show current config")
    parser.add_argument("--link-wallet", action="store_true",
                        help="Link/check the configured external wallet, then exit")
    parser.add_argument("--set", action="append", metavar="KEY=VALUE",
                        help="Update config (e.g., --set entry_threshold=0.08)")
    parser.add_argument("--smart-sizing", action="store_true",
                        help="Use portfolio-based position sizing")
    parser.add_argument("--oracle-latency", action="store_true",
                        help="Run the Binance-vs-Chainlink oracle-latency strategy mode for this cycle")
    parser.add_argument("--execution-route", choices=("direct_clob", "simmer_wallet"),
                        help="Override live execution route for this cycle")
    parser.add_argument("--loop", action="store_true",
                        help="Keep running cycles locally until interrupted")
    parser.add_argument("--scheduled-loop", action="store_true",
                        help="Oracle-latency slot scheduler: sample at window open, then evaluate near close")
    parser.add_argument("--loop-interval", type=int, default=10,
                        help="Seconds between loop/trading-window cycles (default: 10)")
    parser.add_argument("--open-delay", type=float, default=2.0,
                        help="Seconds after each window opens before sampling price-to-beat (default: 2)")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="Only output on trades/errors (ideal for high-frequency runs)")
    args = parser.parse_args()

    # Handle --set (config update)
    if args.set:
        updates = {}
        for item in args.set:
            if "=" not in item:
                print(f"Invalid --set format: {item}. Use KEY=VALUE")
                sys.exit(1)
            key, val = item.split("=", 1)
            if key not in CONFIG_SCHEMA:
                print(f"Unknown config key: {key}")
                print(f"Valid keys: {', '.join(CONFIG_SCHEMA.keys())}")
                sys.exit(1)
            type_fn = CONFIG_SCHEMA[key].get("type", str)
            try:
                if type_fn == bool:
                    updates[key] = val.lower() in ("true", "1", "yes")
                else:
                    updates[key] = type_fn(val)
            except ValueError:
                print(f"Invalid value for {key}: {val}")
                sys.exit(1)
        result = update_config(updates, __file__)
        print(f"✅ Config updated: {updates}")
        sys.exit(0)

    dry_run = not args.live

    # Skip logging for utility commands (--set, --config, --positions, --link-wallet)
    is_utility_mode = args.set or args.config or args.positions or args.link_wallet

    run_folder = None
    structured_log = None
    if not is_utility_mode:
        run_folder, structured_log = setup_run_logging(is_live=args.live)

    # Handle --link-wallet
    if args.link_wallet:
        from api import ensure_wallet_linked_with_retry, get_client
        ok, err = ensure_wallet_linked_with_retry()
        if ok:
            wallet = getattr(get_client(), "_wallet_address", None)
            suffix = f" ({wallet[:10]}...)" if wallet else ""
            print(f"✅ Wallet linked/ready{suffix}")
            sys.exit(0)
        # Redact wallet from error
        text = str(err or "")
        wallet = getattr(get_client(), "_wallet_address", None)
        if wallet:
            text = text.replace(wallet, wallet[:10] + "...")
        print(f"❌ Wallet link failed: {text}")
        sys.exit(2)

    configured_strategy = resolve_config(__file__).get("strategy_mode", "preopen_yes_down").lower()
    effective_oracle_latency = args.oracle_latency or configured_strategy == "oracle_latency"
    effective_preopen = configured_strategy == "preopen_yes_down"

    # Run the strategy (only preopen_yes_down is supported)
    def run_once(suppress_header=False, market_override=None):
        raise RuntimeError(
            f"Unsupported strategy_mode={configured_strategy!r}. "
            "Only preopen_yes_down is supported. "
            "Set SIMMER_FASTLOOP_STRATEGY_MODE=preopen_yes_down in .env"
        )

    cfg = resolve_config(__file__)
    if args.scheduled_loop and not args.config and not args.positions:
        if not effective_oracle_latency:
            print("--scheduled-loop is only supported with oracle_latency strategy mode")
            print("Set SIMMER_FASTLOOP_STRATEGY_MODE=oracle_latency in .env or pass --oracle-latency")
            sys.exit(2)
        run_scheduled_oracle_loop(
            run_once=run_once,
            execution_route=args.execution_route,
            open_delay=args.open_delay,
            trade_interval=args.loop_interval,
        )
    elif effective_preopen:
        run_preopen_loop(run_folder=run_folder, dry_run=dry_run, loop_interval=args.loop_interval, max_events=cfg.get("live_max_events") if not dry_run else None)
    elif args.loop and not args.config and not args.positions:
        interval = max(1, int(args.loop_interval or 10))
        try:
            while True:
                run_once()
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        run_once()

    # Fallback automaton report if strategy returned early (no signal)
    # The strategy emits its own report when it reaches a trade; this covers early exits.
    if os.environ.get("AUTOMATON_MANAGED"):
        import json
        import config as _cfg
        if not _cfg._automaton_reported:
            print(json.dumps({"automaton": {"signals": 0, "trades_attempted": 0, "trades_executed": 0, "skip_reason": "no_signal"}}))


def _fmt_ts(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _sleep_until(target_ts, label, on_tick=None):
    """Sleep until target timestamp, with optional tick callbacks."""
    # Always sleep based on wall-clock time to target, not on background state.
    # Background tasks (settlement checks) should NOT affect sleep duration.
    while True:
        remaining = target_ts - time.time()
        if remaining <= 0:
            return
        print(f"⏳ {label}: sleeping {remaining:.1f}s until {_fmt_ts(target_ts)}")
        sleep_time = min(remaining, 10.0 if on_tick else 60.0)
        time.sleep(sleep_time)
        if on_tick:
            on_tick()


def _settlement_int_env(name, default, minimum=1):
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _queue_settlement_check(pending_settlements, market_info, now=None):
    market_id = market_info.get("market_id")
    if not market_id:
        return
    if any(item.get("market_id") == market_id for item in pending_settlements):
        return

    now = now or time.time()
    initial_delay = _settlement_int_env("SIMMER_FASTLOOP_SETTLEMENT_INITIAL_DELAY_SEC", 180, minimum=30)
    retry_interval = _settlement_int_env("SIMMER_FASTLOOP_SETTLEMENT_RETRY_INTERVAL_SEC", 45, minimum=10)
    max_retries = _settlement_int_env("SIMMER_FASTLOOP_SETTLEMENT_MAX_RETRIES", 20, minimum=1)
    pending_settlements.append({
        "market_id": market_id,
        "slug": market_info.get("slug"),
        "clob_token_ids": market_info.get("clob_token_ids") or [],
        "question": market_info.get("question") or "Unknown Market",
        "trade_event": market_info.get("trade_event"),
        "attempts": 0,
        "max_retries": max_retries,
        "retry_interval": retry_interval,
        "next_check_ts": max(now, float(market_info.get("slot_end") or now) + initial_delay),
    })
    print(
        f"🔍【结算查询】已加入待复查队列；首次复查约在 "
        f"{_fmt_ts(pending_settlements[-1]['next_check_ts'])}；最多 {max_retries} 次"
    )


def _send_settlement_asset_snapshot(item, outcome_str, source):
    trade_event = item.get("trade_event")
    if not isinstance(trade_event, dict) or not trade_event.get("trade_executed"):
        return
    try:
        from trading import get_portfolio
        from notifications import (
            is_configured as feishu_configured,
            send_asset_snapshot_notification_sync,
        )
        if not feishu_configured():
            print("⚠️【结算资产通知】飞书未配置，跳过发送")
            return
        portfolio = get_portfolio()
        ok = send_asset_snapshot_notification_sync(
            portfolio=portfolio,
            trigger_event=item.get("question") or trade_event.get("event_name") or "",
            trade_id=trade_event.get("trade_id") or "",
            scheduled_time=datetime.now(timezone.utc),
            settlement_outcome=outcome_str or "",
            settlement_source=source or "",
            trade_side=trade_event.get("side") or "",
            trade_amount=trade_event.get("amount"),
        )
        if ok:
            print("📲【结算资产通知】飞书已发送当前账号总资产")
        else:
            print("⚠️【结算资产通知】飞书发送失败（不影响交易）")
    except Exception as e:
        print(f"⚠️【结算资产通知】发送异常（不影响交易）：{str(e)[:160]}")


def _poll_pending_settlements(pending_settlements, fetch_market_outcome):
    if not fetch_market_outcome or not pending_settlements:
        return
    now = time.time()
    for item in list(pending_settlements):
        if now < item.get("next_check_ts", 0):
            continue
        item["attempts"] = int(item.get("attempts", 0)) + 1
        attempt = item["attempts"]
        max_retries = int(item.get("max_retries", 1))
        retry_interval = int(item.get("retry_interval", 45))
        outcome_data = fetch_market_outcome(
            item.get("market_id"),
            slug=item.get("slug"),
            clob_token_ids=item.get("clob_token_ids"),
        )
        if outcome_data:
            settled = outcome_data.get("settled")
            outcome_str = outcome_data.get("outcome") or outcome_data.get("winner")
            source = outcome_data.get("source") or "api"
            if settled and outcome_str:
                print(
                    f"🏁【结算结果】{item.get('question', 'Unknown Market')}"
                    f"\n   结果: {outcome_str} ✅"
                    f"\n   状态: 已结算；来源={source}；第 {attempt}/{max_retries} 次复查"
                )
                _send_settlement_asset_snapshot(item, outcome_str, source)
                pending_settlements.remove(item)
                continue
            if not settled:
                if attempt < max_retries:
                    item["next_check_ts"] = now + retry_interval
                    print(
                        f"⏳【结算查询】{item.get('question', 'Unknown Market')}"
                        f"\n   状态: 官方结算尚未同步，第 {attempt}/{max_retries} 次复查；"
                        f"下次约 {_fmt_ts(item['next_check_ts'])}"
                    )
                    continue
                print(
                    f"⏳【结算查询】{item.get('question', 'Unknown Market')}"
                    f"\n   状态: 官方结算仍未同步，已达到本进程复查上限 ({max_retries} 次)"
                )
                pending_settlements.remove(item)
                continue
            print(
                f"❓【结算查询】{item.get('question', 'Unknown Market')}"
                f"\n   结果: {outcome_str or '未知'} (API返回数据不完整)"
            )
            pending_settlements.remove(item)
            continue

        if attempt < max_retries:
            item["next_check_ts"] = now + retry_interval
            print(
                f"⚠️【结算查询】{item.get('question', 'Unknown Market')}"
                f"\n   API 暂未返回结算数据，第 {attempt}/{max_retries} 次复查；"
                f"下次约 {_fmt_ts(item['next_check_ts'])}"
            )
        else:
            print(
                f"⚠️【结算查询】{item.get('question', 'Unknown Market')}"
                f"\n   无法获取结算结果，已达到本进程复查上限 ({max_retries} 次)"
            )
            pending_settlements.remove(item)


def _queue_asset_snapshot(asset_notifications, trade_event, current_slot_end, window_seconds):
    if not isinstance(trade_event, dict) or not trade_event.get("trade_executed"):
        return
    trade_key = trade_event.get("trade_id") or trade_event.get("market_id")
    if not trade_key:
        return
    if any(item.get("trade_key") == trade_key for item in asset_notifications):
        return

    offset_sec = _settlement_int_env("SIMMER_FASTLOOP_ASSET_SNAPSHOT_OFFSET_SEC", 120, minimum=1)
    next_slot_start = current_slot_end
    notify_at = next_slot_start + min(offset_sec, max(1, window_seconds - 1))
    asset_notifications.append({
        "trade_key": trade_key,
        "trade_id": trade_event.get("trade_id") or "",
        "event_name": trade_event.get("event_name") or "Unknown",
        "notify_at": notify_at,
    })
    print(
        f"📲【资产快照】已排队：将在下一事件约 {_fmt_ts(notify_at)} "
        f"发送当前账号总资产"
    )


def _poll_asset_notifications(asset_notifications):
    if not asset_notifications:
        return
    now = time.time()
    for item in list(asset_notifications):
        if now < item.get("notify_at", 0):
            continue
        try:
            from trading import get_portfolio
            from notifications import (
                is_configured as feishu_configured,
                send_asset_snapshot_notification_sync,
            )
            if not feishu_configured():
                print("⚠️【资产快照】飞书未配置，跳过发送")
                asset_notifications.remove(item)
                continue
            portfolio = get_portfolio()
            ok = send_asset_snapshot_notification_sync(
                portfolio=portfolio,
                trigger_event=item.get("event_name") or "",
                trade_id=item.get("trade_id") or "",
                scheduled_time=datetime.now(timezone.utc),
            )
            if ok:
                print("📲【资产快照】飞书资产快照已发送")
            else:
                print("⚠️【资产快照】飞书资产快照发送失败（不影响交易）")
        except Exception as e:
            print(f"⚠️【资产快照】发送异常（不影响交易）：{str(e)[:160]}")
        asset_notifications.remove(item)


def run_scheduled_oracle_loop(run_once, execution_route=None, open_delay=2.0, trade_interval=10):
    """Run oracle latency in slot-aware phases instead of constant polling."""
    cfg = resolve_config(__file__)
    if execution_route:
        cfg["execution_route"] = execution_route

    window_seconds = WINDOW_SECONDS.get(cfg["window"], 300)
    entry_start = int(cfg.get("oracle_entry_start_sec", 30))
    entry_end = int(cfg.get("oracle_entry_end_sec", 3))
    open_delay = max(0.0, float(open_delay or 0.0))
    trade_interval = max(1, int(trade_interval or 10))

    print(
        f"🕒【调度器】window={cfg['window']}；"
        f"开盘采样=+{open_delay:.1f}s；交易窗口=最后 {entry_start}s 到 {entry_end}s；"
        f"评估间隔={trade_interval}s；执行路由={cfg['execution_route']}"
    )

    try:
        from market import discover_fast_market_markets, find_best_fast_market
        from api import lookup_fee_rate, fetch_market_outcome
    except Exception:
        discover_fast_market_markets = None
        find_best_fast_market = None
        lookup_fee_rate = None
        fetch_market_outcome = None

    try:
        from market import start_rtds_stream
    except Exception:
        start_rtds_stream = None

    # Track paper trades for settlement tracking
    pending_settlements = []
    asset_notifications = []

    try:
        while True:
            def poll_background_tasks():
                _poll_pending_settlements(pending_settlements, fetch_market_outcome)
                _poll_asset_notifications(asset_notifications)

            poll_background_tasks()
            now = time.time()
            slot_start = int(now // window_seconds) * window_seconds
            slot_end = slot_start + window_seconds

            # If the current slot's open sample is already gone, wait for next slot.
            open_sample_ts = slot_start + open_delay
            if now > open_sample_ts + max(2, cfg.get("oracle_sample_seconds", 6)):
                slot_start += window_seconds
                slot_end += window_seconds
                open_sample_ts = slot_start + open_delay

            slot_title = (
                f"{datetime.fromtimestamp(slot_start, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}"
                f" -> {datetime.fromtimestamp(slot_end, tz=timezone.utc).strftime('%H:%M:%S')} UTC"
            )
            print("\n" + "█" * 78)
            print(f"【5分钟事件】{slot_title}")
            print("【计划】开盘采一次 Chainlink 基准价；结尾窗口再评估是否下单")
            print("█" * 78)

            if start_rtds_stream:
                start_rtds_stream(cfg["asset"])
                print("📡【RTDS】持久行情连接已启动/复用，后续评估读取 latest tick")

            _sleep_until(open_sample_ts, "等待开盘采样", on_tick=poll_background_tasks)
            cached_market = None
            if discover_fast_market_markets and find_best_fast_market:
                print("🔍【市场缓存】本窗口优先通过 Polymarket deterministic slot 获取 fast market，后续评估复用该事件")
                markets = discover_fast_market_markets(cfg["asset"], cfg["window"], use_simmer=True)
                cached_market = find_best_fast_market(markets, cfg["window"], cfg.get("oracle_entry_end_sec", 3))
                if cached_market:
                    sample_tokens = cached_market.get("clob_token_ids") or []
                    if lookup_fee_rate and sample_tokens and cached_market.get("fee_rate_bps", 0) == 0:
                        fee = lookup_fee_rate(sample_tokens[0])
                        if fee > 0:
                            cached_market["fee_rate_bps"] = fee
                    print(
                        f"✅【市场缓存】{cached_market.get('question')} | "
                        f"condition_id={cached_market.get('condition_id') or 'N/A'} | slug={cached_market.get('slug') or 'N/A'}"
                    )
                else:
                    print(f"⚠️【市场缓存】未找到当前窗口可交易市场，本轮仍交给策略内部发现")

            print("📌【开盘采样】执行一次，用于记录 price_to_beat")
            open_result = run_once(suppress_header=False, market_override=cached_market)

            # Capture market info for settlement tracking (paper trade detected via structured log)
            # CRITICAL: Use condition_id for CLOB API calls, not Simmer's UUID
            _slot_market_info = {
                "slot_end": slot_end,
                "market": cached_market,
                "question": cached_market.get("question") if cached_market else None,
                # condition_id is the Polymarket CLOB identifier (64-char hex) - use for API calls
                # slug/market_id are for display only
                "condition_id": cached_market.get("condition_id") if cached_market else None,
                "slug": cached_market.get("slug") if cached_market else None,
                "market_id": (
                    cached_market.get("condition_id")  # Primary: Polymarket CLOB identifier
                    or cached_market.get("slug")  # Fallback: slug
                # Note: Simmer's UUID (market_id) should NOT be used for CLOB API
                ) if cached_market else None,
                "clob_token_ids": cached_market.get("clob_token_ids") if cached_market else None,
                "trade_event": None,
            }

            trade_start_ts = slot_end - entry_start
            trade_stop_ts = slot_end - entry_end
            _sleep_until(trade_start_ts, "等待结尾交易窗口", on_tick=poll_background_tasks)

            while time.time() <= trade_stop_ts:
                print("\n" + "─" * 72)
                print("🎯【结尾交易评估】检查延迟信号、价格、风控，并决定是否下单")
                print("─" * 72)
                trade_result = run_once(suppress_header=True, market_override=cached_market)
                if isinstance(trade_result, dict) and trade_result.get("trade_executed"):
                    _slot_market_info["trade_event"] = trade_result
                sleep_for = min(trade_interval, max(0.0, trade_stop_ts - time.time()))
                if sleep_for > 0:
                    time.sleep(sleep_for)

            print("⏭️【事件结束】本 5m 窗口处理完毕，进入下一个窗口")

            # ── Settlement tracking ──────────────────────────────────────────
            if fetch_market_outcome and _slot_market_info.get("market_id"):
                _queue_settlement_check(pending_settlements, _slot_market_info)
                _poll_pending_settlements(pending_settlements, fetch_market_outcome)
            _poll_asset_notifications(asset_notifications)
    except KeyboardInterrupt:
        print("\nStopped.")


def run_preopen_loop(*, run_folder: str | Path, dry_run: bool = True, loop_interval: int = 60, max_events: int | None = None):
    """
    Run the preopen_yes_down strategy loop.

    Each cycle:
      1. Build event pool (discover → filter → GC)
      2. Select nearest unstarted event
      3. Execute YES + Down hedge per state machine
      4. Log decision + state
      5. Sleep for poll_interval seconds
    """
    cfg = resolve_config(__file__)
    asset = cfg.get("asset", "BTC").upper()
    window = cfg.get("window", "5m")
    poll_interval = cfg.get("preopen_poll_interval_sec", 60)
    lead_time_sec = cfg.get("preopen_lead_time_sec", 600)
    gc_grace_sec = cfg.get("preopen_gc_grace_sec", 60)
    settlement_interval_sec = int(cfg.get("settlement_poll_interval_sec", 120))
    next_settlement_check = time.time() + settlement_interval_sec
    mode_label = "模拟交易" if dry_run else "实盘交易"
    print(f"\n{'═' * 60}")
    print(f"  【 PreOpen Loop 预开盘循环 】")
    print(f"{'═' * 60}")
    print(f"  模式: {mode_label}")
    print(f"  品种: {asset} | 窗口: {window}")
    print(f"  ── 轮询参数 ──")
    print(f"  • 轮询间隔: {poll_interval}s")
    print(f"  • 预开盘提前时间 (lead_time): {lead_time_sec}s")
    print(f"  • 垃圾回收宽限期 (gc_grace): {gc_grace_sec}s")
    print(f"  ── YES 主单参数 ──")
    print(f"  • 买入数量: {cfg.get('preopen_yes_shares_x')} 份")
    print(f"  • 最高价格: ${cfg.get('preopen_yes_max_price')}")
    print(f"  ── NO 对冲参数 ──")
    print(f"  • 静态对冲价格: ${cfg.get('preopen_down_resting_price')}")
    print(f"  • 切换 TTL 阈值: {cfg.get('preopen_down_switch_ttl_sec')}s")
    print(f"  • 最大 FAK 价格: ${cfg.get('preopen_down_fak_max_price')}")
    print(f"  • 最小套利边缘: {cfg.get('preopen_min_arb_edge')}")
    print(f"  • live 事件上限: {cfg.get('live_max_events')}")
    print(f"{'─' * 60}")

    pool = PreopenEventPool()
    cycle = 0

    processed_events: set[str] = set()

    try:
        while True:
            cycle += 1
            now = datetime.now(timezone.utc)
            print(f"\n{'━' * 60}")
            print(f"  【 PreOpen 轮次 #{cycle} 】 {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")

            # ── 0. GC before pool refresh (clean up events that became stale ─────────
            #     or untradeable since the last cycle, before we spend time discovering)
            pre_removed = pool.gc(now, gc_grace_sec)
            if pre_removed:
                for e in pre_removed:
                    print(f"  🗑 Pre-GC removed: {e.slug[:50]} | reason=stale/untradeable")

            # ── 1. Build / refresh event pool ────────────────────────────────
            discovered, removed, pool_size = build_event_pool(
                pool=pool,
                asset=asset,
                window=window,
                lead_time_sec=lead_time_sec,
                gc_grace_sec=gc_grace_sec,
                now=now,
            )

            if removed:
                for e in removed:
                    print(f"  🗑 GC 移除: {e.slug[:50]} | 状态={e.state.value}")
            if discovered:
                for e in discovered:
                    print(f"  ✨ 新发现事件: {format_event_summary(e, now)}")

            if not dry_run and max_events is not None and len(processed_events) >= max_events:
                print(f"  ✅ 已达到 live 事件上限: {max_events}，停止交易。")
                return

            # ── 2. Select nearest event ──────────────────────────────────────
            target = pool.select_nearest(now, lead_time_sec)
            if target is None:
                print("  ⏳ 事件池中无可交易事件，等待下次轮询")
            else:
                tts = target.time_to_start(now)
                print(f"  🎯 目标事件: {format_event_summary(target, now)}")
                print(f"  ⏱  距离开始={tts:.1f}s | 状态={target.state.value} | 动作次数={target.action_count}")

                # ── 3. Execute cycle ─────────────────────────────────────────
                actions = execute_event_cycle(target, pool, dry_run=dry_run, now=now)

                for a in actions:
                    # 动作中文映射
                    action_map = {
                        "yes_entry": "📗 YES下单",
                        "down_resting": "📙 NO挂单",
                        "down_switch": "📙 NO切换FAK",
                        "cycle": "循环检查",
                    }
                    action_cn = action_map.get(a.action, a.action)

                    # 结果中文映射
                    result_map = {
                        "placed": "✅ 已下单",
                        "replaced": "✅ 已替换",
                        "skipped": "⏭️ 跳过",
                        "cancelled": "❌ 已取消",
                        "error": "❌ 错误",
                    }
                    result_cn = result_map.get(a.result.value, a.result.value)

                    price_str = f"${a.price:.4f}" if a.price else "-"
                    shares_str = f"{a.shares}份" if a.shares else "-"
                    oid_str = f"订单ID={a.order_id}" if a.order_id else f"原因: {a.error}" if a.error else ""

                    print(f"  {action_cn}: {result_cn} | 价格={price_str} | 数量={shares_str} | {oid_str}")

                    if a.result.value in ("placed", "replaced") and a.shares and a.price:
                        side_label = "YES" if a.action == "yes_entry" else "NO" if a.action in ("down_resting", "down_switch") else ""
                        if side_label:
                            prefix = "[PAPER] " if dry_run else ""
                            print(f"  ✅ {prefix}Bought {a.shares:.1f} {side_label} shares @ ${a.price:.3f}")
                            try:
                                export_preopen_action_to_excel(
                                    run_folder=run_folder,
                                    event=target,
                                    action=a,
                                    dry_run=dry_run,
                                    execution_route=cfg.get("execution_route"),
                                    wallet_address=getattr(get_client(live=True), "_wallet_address", None),
                                )
                            except Exception:
                                pass

                # mark one completed live event
                if not dry_run and max_events is not None and target and any(a.result.value in ("placed", "replaced") for a in actions):
                    processed_events.add(target.condition_id)
                    print(f"  🎯 live 事件进度: {len(processed_events)}/{max_events}")

                # ── 4. 打印下单后的事件状态 ─────────────────────────────────────
                if target.state.value in ("yes_placed", "down_resting", "down_switched"):
                    print(f"\n  ✅ 【已下单】{target.slug[:40]}")
                    yes_price = f"{target.yes_fill_price:.4f}" if target.yes_fill_price else "N/A"
                    down_price = f"{target.down_fill_price:.4f}" if target.down_fill_price else "N/A"
                    if target.yes_order_id:
                        print(f"     📗 YES: 订单ID={target.yes_order_id} | 成交价=${yes_price}")
                    if target.down_order_id:
                        print(f"     📙 NO: 订单ID={target.down_order_id} | 成交价=${down_price}")
                    print(f"     📍 当前状态: {target.state.value} | 动作次数: {target.action_count}")

                # ── 5. Log arb edge for DOWN_RESTING → DOWN_SWITCHED decision ─
                if target.state.value in ("down_resting", "down_switched"):
                    if target.yes_fill_price:
                        from api import fetch_side_orderbook_price as _fetch
                        arb = evaluate_arb_edge(
                            yes_fill_price=target.yes_fill_price,
                            yes_side_ask=target.yes_fill_price,
                            no_side_ask=cfg.get("preopen_down_resting_price", 0.40),
                            fee_rate_bps=target.fee_rate_bps,
                            min_arb_edge=cfg.get("preopen_min_arb_edge", 0.01),
                        )
                        print(
                            f"  📊 套利边缘: net={arb.net_edge:.4f} | "
                            f"充足={arb.sufficient} | {arb.reason}"
                        )

            # ── Settlement reconciliation (every 2 minutes, best-effort) ─────
            now_ts = time.time()
            if run_folder and now_ts >= next_settlement_check:
                try:
                    # Append-only updates to trades.csv for newly-settled rows
                    from state.settlement_reconciler import reconcile_once

                    appended = reconcile_once(run_folder=run_folder)
                    if appended:
                        print(f"  🧾 结算回填: 已追加 {appended} 条 settled 记录到 trades.csv")
                except Exception:
                    pass
                next_settlement_check = now_ts + settlement_interval_sec

            # ── 5. Sleep until next poll ────────────────────────────────────
            print(f"\n  💤 等待 {poll_interval}s 后进行下次轮询...")
            time.sleep(poll_interval)

    except KeyboardInterrupt:
        print("\n【PreOpen Loop】Stopped by user.")


if __name__ == "__main__":
    main()
