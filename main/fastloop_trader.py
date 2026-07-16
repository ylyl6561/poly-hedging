"""
Simmer FastLoop Trading Skill — CLI entry point.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_project_root = Path(__file__).parent.parent.resolve()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

sys.stdout.reconfigure(line_buffering=True)

from core import load_env_file, resolve_config, update_config, CONFIG_SCHEMA
from core.config import ACTIVE_WINDOWS, ACTIVE_WINDOW, resolve_window_overrides
from core.constants import WINDOW_SECONDS
from api import install_sdk_logger_noise_filter
from state import StructuredRunLog
from state.trade_state import init_trade_state, get_trade_state_manager, get_async_outcome_poller, TradePhase
from state.global_trade_event_journal import GlobalTradeEventJournal
from market import discover_fast_market_markets
from strategy.dual_wallet_event_strategy import DualWalletEventStrategy

load_env_file(__file__)
install_sdk_logger_noise_filter()


def setup_run_logging(is_live: bool):
    project_root = Path(__file__).parent
    runs_dir = project_root / "runs"
    runs_dir.mkdir(exist_ok=True)
    mode_suffix = "live" if is_live else "dryrun"
    timestamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    run_folder = runs_dir / f"{timestamp}_{mode_suffix}"
    run_folder.mkdir(exist_ok=True)

    log_file = run_folder / "output.log"
    structured_log = StructuredRunLog(run_folder, mode="live" if is_live else "dryrun", log_file_name=log_file.name)

    class TimestampLineTee:
        def __init__(self, original_stream, log_file, structured_log=None):
            self.original_stream = original_stream
            self.log_file = open(log_file, "a", buffering=1, encoding="utf-8")
            self.structured_log = structured_log
            self._line_buffer = ""
            self.encoding = getattr(original_stream, "encoding", "utf-8")

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

    sys.stdout = TimestampLineTee(sys.stdout, log_file, structured_log)
    sys.stderr = TimestampLineTee(sys.stderr, log_file, structured_log)

    print(f"📁 日志目录: {run_folder}")
    print(f"📄 日志文件: {log_file}")
    print("=" * 60)
    return run_folder, structured_log


def _pick_events(limit: int, window: str):
    """Discover upcoming markets for a single event window.

    Polymarket publishes deterministic slugs for the current 5m/15m/1h slot;
    we filter by window so the caller only sees markets matching the window
    this strategy is dedicated to.  Upcoming markets are returned sorted by
    start time so the closest upcoming event is processed first.
    """
    markets = discover_fast_market_markets(asset="BTC", window=window, use_simmer=True)
    now = datetime.now(timezone.utc)
    upcoming_markets = []
    for market in markets:
        start_time, end_time = _build_event_times(market, window=window)
        if not start_time or not end_time:
            continue
        if start_time <= now:
            continue
        upcoming_markets.append((start_time, market))
    upcoming_markets.sort(key=lambda item: item[0])
    return [market for _, market in upcoming_markets[:limit]]


def _build_event_times(market: dict, window: str = "5m"):
    """
    构建事件的 start_time 和 end_time。

    注意：market.get("end_time") 可能返回：
    1. timezone-aware datetime（来自 Simmer SDK 或 deterministic slot 解析）
    2. Unix 时间戳（来自某些 API）

    当返回的是 datetime 对象时，它应该是 UTC 或带有时区信息的。
    我们不能简单地用 fromtimestamp 转换。

    ``window`` controls the duration subtracted from ``end_time`` to derive
    ``start_time``.  Defaults to ``5m`` for backward compatibility, but should
    always be passed when handling 15m / 1h events.
    """
    end_time = market.get("end_time")
    if end_time is None:
        return None, None

    window_seconds = WINDOW_SECONDS.get(window, WINDOW_SECONDS.get("5m", 300))

    # 如果已经是 datetime 对象（可能是 naive 或 aware）
    if isinstance(end_time, datetime):
        # 如果是 naive datetime，假设它是 UTC
        if end_time.tzinfo is None:
            end_time_utc = end_time.replace(tzinfo=timezone.utc)
        else:
            # 转换为 UTC
            end_time_utc = end_time.astimezone(timezone.utc)
        start_time_utc = end_time_utc - timedelta(seconds=window_seconds)
        return start_time_utc, end_time_utc

    # 如果是 Unix 时间戳（秒或毫秒）
    try:
        ts = float(end_time)
        # 处理毫秒时间戳
        if ts > 1e12:
            ts = ts / 1000
        end_time_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
        start_time_utc = end_time_utc - timedelta(seconds=window_seconds)
        return start_time_utc, end_time_utc
    except (TypeError, ValueError):
        return None, None


def main():
    parser = argparse.ArgumentParser(description="Dual-wallet event trading")
    parser.add_argument("--live", action="store_true", help="Execute real trades")
    parser.add_argument("--set", action="append", metavar="KEY=VALUE", help="Update config")
    parser.add_argument("--once", action="store_true", help="Run one polling cycle and exit")
    parser.add_argument("--quiet", "-q", action="store_true", help="Only output on trades/errors")
    parser.add_argument("--no-async", action="store_true", help="Disable async outcome polling")
    parser.add_argument("--mock", action="store_true", help="Mock mode: simulate orders without real execution")
    parser.add_argument("--mock-fill-side", default="UP", choices=["UP", "DOWN"], help="Mock: which side fills first")
    parser.add_argument("--mock-fill-after", type=float, default=5.0, help="Mock: seconds until fill")
    parser.add_argument(
        "--window",
        choices=["5m", "15m", "1h"],
        help="Override the active window for this run (single-window mode). "
             "Mutually exclusive with SIMMER_FASTLOOP_WINDOWS env var.",
    )
    args = parser.parse_args()

    if args.set:
        updates = {}
        for item in args.set:
            if "=" not in item:
                print(f"Invalid --set format: {item}. Use KEY=VALUE")
                sys.exit(1)
            key, val = item.split("=", 1)
            if key not in CONFIG_SCHEMA:
                print(f"Unknown config key: {key}")
                sys.exit(1)
            type_fn = CONFIG_SCHEMA[key].get("type", str)
            try:
                updates[key] = val.lower() in ("true", "1", "yes") if type_fn == bool else type_fn(val)
            except ValueError:
                print(f"Invalid value for {key}: {val}")
                sys.exit(1)
        update_config(updates, __file__)
        print(f"✅ Config updated: {updates}")
        sys.exit(0)

    dry_run = not args.live
    run_folder, structured_log = setup_run_logging(is_live=args.live)
    cfg = resolve_config(__file__)

    # Decide which event windows to process.  Priority:
    # 1. --window CLI override (single-window mode for ad-hoc testing)
    # 2. SIMMER_FASTLOOP_WINDOWS env / config.json (multi-window default)
    # In every case the result is a non-empty list of window labels.
    if args.window:
        active_windows = [args.window]
    else:
        active_windows = list(ACTIVE_WINDOWS) if ACTIVE_WINDOWS else ["5m"]
    if ACTIVE_WINDOW and ACTIVE_WINDOW not in active_windows:
        print(
            f"⚠️  SIMMER_FASTLOOP_ACTIVE_WINDOW={ACTIVE_WINDOW} is set, "
            f"but multi-window mode is active ({active_windows}). "
            "Single-window env settings are ignored. "
            "Use --window to force single-window mode."
        )

    print(f"🪟 活跃窗口: {active_windows}")

    # 初始化交易状态管理器
    state_manager = init_trade_state()

    # 初始化异步轮询器
    async_poller = get_async_outcome_poller()
    if not args.no_async:
        async_poller.start()

    # 创建策略（支持 mock 模式，每个窗口独立实例）
    strategies: dict[str, DualWalletEventStrategy] = {}
    for window in active_windows:
        strategies[window] = DualWalletEventStrategy(
            run_folder=run_folder,
            dry_run=dry_run,
            config=cfg,
            structured_log=structured_log,
            mock_mode=args.mock,
            mock_fill_side=args.mock_fill_side,
            mock_fill_after_sec=args.mock_fill_after,
            window=window,
        )

    def run_once_for_window(window: str) -> None:
        """Run one polling cycle for a single event window.

        每次只处理一个窗口的 markets 和 task。状态管理器是全局共享的，
        所以 5m 和 15m 不会重复挂同一个事件，但每个窗口的 strategy /
        TaskManager 实例独立跟踪损失窗口与停机决策。

        ``window_cfg`` 已经把 ``dual_wallet_*`` 的全局配置 + 该窗口的
        ``WINDOW_BASELINE_CONFIG`` 合并好了，所以这里统一使用规范化的
        字段名（不带 ``dual_wallet_`` 前缀）即可。
        """
        strategy = strategies[window]
        # Resolve per-window config once per cycle to avoid per-iteration lookups.
        window_cfg = resolve_window_overrides(cfg, window)
        effective_query_limit = int(
            window_cfg.get("dual_wallet_event_query_limit", cfg.get("dual_wallet_event_query_limit", 20))
        )

        markets = _pick_events(effective_query_limit, window=window)
        structured_log.record_markets(markets)

        new_count = 0
        skip_count = 0

        for market in markets:
            if strategy.should_halt():
                halt_reason = strategy.halt_reason() or "max_consecutive_losses"
                print(f"\n⚠️ [{window}] 停机: {halt_reason}")
                structured_log.set_halted(True)
                structured_log.record_event(event_name=market.get("question") or market.get("slug") or "Unknown Event", event_id=market.get("condition_id") or "", phase="halted", payload={"reason": halt_reason, "window": window})
                structured_log.flush()
                break

            event_name = market.get("question") or market.get("slug") or "Unknown Event"
            condition_id = market.get("condition_id") or ""

            # 检查是否已经在处理中
            existing_trade = state_manager.get_trade(condition_id)
            if existing_trade and existing_trade.phase not in (TradePhase.COMPLETED.value, TradePhase.FAILED.value):
                if not args.quiet:
                    print(f"[跳过] [{window}] {event_name}: 已在处理中 (phase={existing_trade.phase})")
                skip_count += 1
                continue

            clob_token_ids = market.get("clob_token_ids") or []
            start_time, end_time = _build_event_times(market, window=window)
            if not start_time or not end_time:
                continue

            now = datetime.now(timezone.utc)
            # min_seconds_before_start 语义（统一公式：time_to_start >= N）：
            #   N > 0：要求距离 start_time 至少 N 秒前才挂单
            #   N == 0：刚好到 start_time 才挂单
            #   N < 0：允许事件已开始最多 |N| 秒仍挂单（即 time_to_start >= N，
            #          time_to_start 为负时只要大于 N 即仍在 |N| 秒窗口内）
            time_to_start = (start_time - now).total_seconds()
            min_before_start = int(window_cfg["min_seconds_before_start"])
            if time_to_start < min_before_start:
                skip_count += 1
                if min_before_start >= 0:
                    print(f"[跳过] [{window}] {event_name}: 距开始 {time_to_start:.0f}s < {min_before_start}s")
                else:
                    # 负数语义：事件已开始超过 |N| 秒
                    elapsed_after_start = -time_to_start
                    print(f"[跳过] [{window}] {event_name}: 已开始 {elapsed_after_start:.0f}s > {abs(min_before_start)}s (min_seconds_before_start={min_before_start})")
                continue

            new_count += 1
            print(f"【事件】[{window}] {event_name}")
            structured_log.record_event(event_name=event_name, event_id=condition_id, phase="start", payload={"market": {"slug": market.get("slug"), "condition_id": condition_id, "source": market.get("source")}, "start_time": start_time.isoformat(), "end_time": end_time.isoformat(), "window": window})
            summary = strategy.run_event(
                event_name=event_name,
                event_id=condition_id,
                start_time=start_time,
                end_time=end_time,
                clob_token_ids=clob_token_ids,
                fee_rate_bps=int(market.get("fee_rate_bps") or 0),
                condition_id=condition_id,
                # 下面这些参数全部沿用统一的 dual_wallet_* 命名 —— 5m/15m/1h
                # 之间的差异已经由 resolve_window_overrides 自动应用。
                entry_shares=float(window_cfg["entry_shares"]),
                up_price=float(window_cfg["entry_up_price"]),
                down_price=float(window_cfg["entry_down_price"]),
            )
            structured_log.record_result(summary)
            structured_log.record_event_state(event_name=event_name, event_id=condition_id, flow_state="finished", wallet_status={wallet_id: f"{pnl:.4f}" for wallet_id, pnl in summary.wallet_pnl_usd.items()}, note=f"profit={summary.is_profit};window={window}")
            structured_log.flush()

            if strategy.should_halt():
                structured_log.set_halted(True)
                print(f"\n⚠️ [{window}] 停机: {strategy.halt_reason() or 'unknown_reason'}")
                break

        if new_count > 0 or skip_count > 0:
            print(f"[{window}] 轮询完成: 新事件={new_count}, 跳过={skip_count}")

    def run_once():
        """Run one polling cycle across all active windows."""
        for window in active_windows:
            run_once_for_window(window)

        summary = state_manager.get_summary()
        if summary["active_count"] > 0:
            print(f"📊 活跃交易: {summary['active_count']} | 总交易: {summary['total_trades']}")

    if args.once:
        run_once()
    else:
        try:
            # 用最严的那个窗口的 poll_interval 作为外层节流，保证所有窗口都被
            # 频繁扫描。 5m 窗口 baseline 是 0.1s，所以外层 sleep 至少 0.1s。
            base_poll = float(cfg.get("dual_wallet_poll_interval_sec", 5))
            per_window_polls = [
                resolve_window_overrides(cfg, w).get("poll_interval_sec", base_poll)
                for w in active_windows
            ]
            outer_sleep = max(0.05, min(per_window_polls + [base_poll]))
            while True:
                run_once()
                time.sleep(outer_sleep)
        except KeyboardInterrupt:
            print("\nStopped.")
        finally:
            if not args.no_async:
                async_poller.stop()


if __name__ == "__main__":
    main()
