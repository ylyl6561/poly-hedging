"""
Simmer FastLoop Trading Skill — CLI entry point.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_project_root = Path(__file__).parent.parent.resolve()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

sys.stdout.reconfigure(line_buffering=True)

from core import load_env_file, resolve_config, update_config, CONFIG_SCHEMA
from state import StructuredRunLog
from market import discover_fast_market_markets
from strategy.dual_wallet_event_strategy import DualWalletEventStrategy

load_env_file(__file__)


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


def _pick_events(limit: int):
    markets = discover_fast_market_markets(asset="BTC", window="5m", use_simmer=True)
    now = datetime.now(timezone.utc)
    upcoming_markets = []
    for market in markets:
        start_time, end_time = _build_event_times(market)
        if not start_time or not end_time:
            continue
        if start_time <= now:
            continue
        upcoming_markets.append((start_time, market))
    upcoming_markets.sort(key=lambda item: item[0])
    return [market for _, market in upcoming_markets[:limit]]


def _build_event_times(market: dict):
    end_time = market.get("end_time")
    if end_time is None:
        return None, None
    start_time = datetime.fromtimestamp(end_time.timestamp() - 300, tz=timezone.utc)
    return start_time, end_time


def main():
    parser = argparse.ArgumentParser(description="Dual-wallet event trading")
    parser.add_argument("--live", action="store_true", help="Execute real trades")
    parser.add_argument("--set", action="append", metavar="KEY=VALUE", help="Update config")
    parser.add_argument("--loop", action="store_true", help="Keep running until interrupted")
    parser.add_argument("--quiet", "-q", action="store_true", help="Only output on trades/errors")
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
    strategy = DualWalletEventStrategy(run_folder=run_folder, dry_run=dry_run, config=cfg, structured_log=structured_log)

    def run_once():
        markets = _pick_events(int(cfg.get("dual_wallet_event_query_limit", 20)))
        structured_log.record_markets(markets)
        if not markets:
            print("No markets found.")
            return
        for market in markets:
            if strategy.should_halt():
                halt_reason = strategy.halt_reason() or "max_consecutive_losses"
                print(f"【停机】{halt_reason}，已停止所有交易")
                structured_log.set_halted(True)
                structured_log.record_event(event_name=market.get("question") or market.get("slug") or "Unknown Event", event_id=market.get("condition_id") or "", phase="halted", payload={"reason": halt_reason})
                structured_log.flush()
                break

            event_name = market.get("question") or market.get("slug") or "Unknown Event"
            condition_id = market.get("condition_id") or ""
            clob_token_ids = market.get("clob_token_ids") or []
            start_time, end_time = _build_event_times(market)
            if not start_time or not end_time:
                continue
            if datetime.now(timezone.utc) >= start_time:
                print(f"【跳过】{event_name}：当前事件已开始，等待下一个未开始事件")
                structured_log.record_event(
                    event_name=event_name,
                    event_id=condition_id,
                    phase="skipped_started",
                    payload={
                        "market": {"slug": market.get("slug"), "condition_id": condition_id, "source": market.get("source")},
                        "start_time": start_time.isoformat(),
                        "end_time": end_time.isoformat(),
                        "reason": "event_already_started",
                    },
                )
                continue

            print(f"【事件】{event_name}")
            structured_log.record_event(event_name=event_name, event_id=condition_id, phase="start", payload={"market": {"slug": market.get("slug"), "condition_id": condition_id, "source": market.get("source")}, "start_time": start_time.isoformat(), "end_time": end_time.isoformat()})
            summary = strategy.run_event(
                event_name=event_name,
                event_id=condition_id,
                start_time=start_time,
                end_time=end_time,
                clob_token_ids=clob_token_ids,
                fee_rate_bps=int(market.get("fee_rate_bps") or 0),
                condition_id=condition_id,
                amount_usd=float(cfg.get("dual_wallet_entry_amount_usd", 10.0)),
                up_price=float(cfg.get("dual_wallet_entry_up_price", 0.5)),
                down_price=float(cfg.get("dual_wallet_entry_down_price", 0.5)),
            )
            structured_log.record_result(summary)
            structured_log.record_event_state(event_name=event_name, event_id=condition_id, flow_state="finished", wallet_status={wallet_id: f"{pnl:.4f}" for wallet_id, pnl in summary.wallet_pnl_usd.items()}, note=f"profit={summary.is_profit}")
            structured_log.flush()
            print(f"【事件结果】{event_name}：{'盈利' if summary.is_profit else '亏损'}；总收益={summary.total_pnl_usd:.4f}")

    if args.loop:
        try:
            while True:
                run_once()
                time.sleep(int(cfg.get("dual_wallet_poll_interval_sec", 5)))
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        run_once()


if __name__ == "__main__":
    main()
