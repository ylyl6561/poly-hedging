"""
Sidecar structured JSON output for FastLoop run logs.

This module intentionally consumes the same human-readable log lines that are
already written to output.log, so strategy and execution behavior remain
unchanged.
"""

import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


class StructuredRunLog:
    """Incrementally parse output.log lines and persist a JSON sidecar."""

    def __init__(self, run_folder, mode, log_file_name="output.log"):
        self.run_folder = Path(run_folder)
        self.path = self.run_folder / "structured_output.json"
        self.data = {
            "schema_version": 1,
            "run": {
                "mode": mode,
                "run_folder": str(self.run_folder),
                "log_file": str(self.run_folder / log_file_name),
                "started_at": None,
                "updated_at": None,
            },
            "scheduler": {},
            "config": {},
            "events": [],
            "trades": [],
            "results": [],
            "summary": {},
        }
        self._current_event = None
        self._current_price = None
        self._current_judgement = None
        self._pending_execution = None
        self._line_no = 0
        self._dirty = True
        self._changes_since_flush = 0
        self._last_flush_monotonic = 0.0

    def record_line(self, timestamp, line):
        self._line_no += 1
        if self.data["run"]["started_at"] is None:
            self.data["run"]["started_at"] = timestamp
        self.data["run"]["updated_at"] = timestamp

        trades_before = len(self.data["trades"])
        results_before = len(self.data["results"])
        changed = False
        changed |= self._parse_scheduler(line)
        changed |= self._parse_config(line)
        changed |= self._parse_event(timestamp, line)
        changed |= self._parse_market_state(line)
        changed |= self._parse_signal(line)
        changed |= self._parse_price(line)
        changed |= self._parse_execution_plan(line)
        changed |= self._parse_result(timestamp, line)
        changed |= self._parse_trade(timestamp, line)

        if changed:
            self._refresh_summary()
            self._dirty = True
            self._changes_since_flush += 1
            if self._should_flush_now(trades_before, results_before, line):
                self.flush()

    def flush(self):
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=str(self.path.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.replace(tmp_name, self.path)
            self._dirty = False
            self._changes_since_flush = 0
            self._last_flush_monotonic = time.monotonic()
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def _should_flush_now(self, trades_before, results_before, line):
        if len(self.data["trades"]) > trades_before:
            return True
        if len(self.data["results"]) > results_before and (
            "纸面成交" in line or "真实成交" in line or "失败" in line
        ):
            return True
        if "【调度器】" in line or "📄 日志文件:" in line:
            return True
        if self._changes_since_flush >= 50:
            return True
        if time.monotonic() - self._last_flush_monotonic >= 30:
            return True
        return False

    def _event_key(self):
        event = self._current_event or {}
        return (event.get("window_start_utc"), event.get("window_end_utc"), event.get("market_id"))

    def _ensure_event(self):
        if not self._current_event:
            return None
        key = self._event_key()
        for event in self.data["events"]:
            if (event.get("window_start_utc"), event.get("window_end_utc"), event.get("market_id")) == key:
                return event
        event = dict(self._current_event)
        event.setdefault("observations", [])
        event.setdefault("results", [])
        event.setdefault("trades", [])
        self.data["events"].append(event)
        return event

    def _parse_scheduler(self, line):
        match = re.search(
            r"【调度器】window=([^；]+)；开盘采样=\+([0-9.]+)s；交易窗口=最后 ([0-9]+)s 到 ([0-9]+)s；评估间隔=([0-9]+)s；执行路由=(.+)",
            line,
        )
        if not match:
            return False
        self.data["scheduler"] = {
            "window": match.group(1),
            "open_delay_sec": float(match.group(2)),
            "entry_start_sec": int(match.group(3)),
            "entry_end_sec": int(match.group(4)),
            "trade_interval_sec": int(match.group(5)),
            "execution_route": match.group(6).strip(),
        }
        return True

    def _parse_config(self, line):
        patterns = {
            "asset": r"Asset:\s+(.+)",
            "window": r"Window:\s+(.+)",
            "strategy_mode": r"Strategy mode:\s+(.+)",
            "execution_route": r"Execution route:\s+(.+)",
            "max_position_usd": r"Max position:\s+\$([0-9.]+)",
            "max_entry_price": r"Max entry price:\s+\$([0-9.]+)",
            "min_binance_edge_usd": r"Min Binance edge:\s+\$([0-9.]+)",
            "min_directional_gap_usd": r"Min directional gap:\s+\$([0-9.]+)",
            "min_chainlink_margin_usd": r"Min Chainlink margin:\s+\$([0-9.]+)",
            "min_feed_lag_ms": r"Min feed lag:\s+([0-9]+)ms",
            "min_payout_edge": r"Min payout edge:\s+([0-9.]+)",
        }
        for key, pattern in patterns.items():
            match = re.search(pattern, line)
            if not match:
                continue
            value = match.group(1).strip()
            if key.endswith("_usd") or key in {"max_entry_price", "min_payout_edge"}:
                value = float(value)
            elif key.endswith("_ms"):
                value = int(value)
            self.data["config"][key] = value
            return True
        return False

    def _parse_event(self, timestamp, line):
        title = re.search(r"【事件】(.+)", line)
        if title:
            self._current_event = {
                "question": title.group(1).strip(),
                "first_seen_at": timestamp,
            }
            self._current_price = None
            self._current_judgement = None
            return True

        window = re.search(r"【窗口】(.*?) UTC -> (.*?) UTC", line)
        if window and self._current_event is not None:
            self._current_event["window_start_utc"] = window.group(1)
            self._current_event["window_end_utc"] = window.group(2)
            self._ensure_event()
            return True

        source = re.search(r"【市场源】([^|]+)\s*\|\s*market_id=(.+)", line)
        if source and self._current_event is not None:
            self._current_event["market_source"] = source.group(1).strip()
            self._current_event["market_id"] = source.group(2).strip()
            event = self._ensure_event()
            if event:
                event.update({
                    "market_source": self._current_event["market_source"],
                    "market_id": self._current_event["market_id"],
                })
            return True
        return False

    def _parse_market_state(self, line):
        event = self._ensure_event()
        if not event:
            return False

        current_yes = re.search(r"Current YES price: \$([0-9.]+)", line)
        if current_yes:
            event["last_yes_price"] = float(current_yes.group(1))
            return True

        price_to_beat = re.search(r"Price to beat: \$([0-9,]+\.[0-9]+)", line)
        if price_to_beat:
            event["price_to_beat"] = _to_float(price_to_beat.group(1))
            return True

        binance = re.search(r"Binance:\s+\$([0-9,]+\.[0-9]+) \(age ([0-9]+)ms, ts ([0-9]+)\)", line)
        if binance:
            event["last_binance"] = {
                "price": _to_float(binance.group(1)),
                "age_ms": int(binance.group(2)),
                "timestamp_ms": int(binance.group(3)),
            }
            return True

        chainlink = re.search(r"Chainlink: \$([0-9,]+\.[0-9]+) \(age ([0-9]+)ms, ts ([0-9]+)\)", line)
        if chainlink:
            event["last_chainlink"] = {
                "price": _to_float(chainlink.group(1)),
                "age_ms": int(chainlink.group(2)),
                "timestamp_ms": int(chainlink.group(3)),
            }
            return True
        return False

    def _parse_signal(self, line):
        event = self._ensure_event()
        if not event:
            return False
        match = re.search(
            r"【判断】Binance 相对基准价=([+-]?[0-9.]+)；Chainlink 相对基准价=([+-]?[0-9.]+)；"
            r"Binance-Chainlink 价差=([+-]?[0-9.]+)；方向性领先=([+-]?[0-9.]+)；"
            r"时间戳领先=([+-]?[0-9]+)ms；候选方向=(YES|NO)",
            line,
        )
        if not match:
            return False
        self._current_judgement = {
            "binance_margin_usd": float(match.group(1)),
            "chainlink_margin_usd": float(match.group(2)),
            "feed_gap_usd": float(match.group(3)),
            "directional_feed_lead_usd": float(match.group(4)),
            "feed_lag_ms": int(match.group(5)),
            "candidate_side": match.group(6),
        }
        event["last_judgement"] = dict(self._current_judgement)
        return True

    def _parse_price(self, line):
        event = self._ensure_event()
        if not event:
            return False
        match = re.search(
            r"【价格】准备买 (YES|NO)；入场价=\$([0-9.]+)；YES中间价=\$([0-9.]+)；价格源=([^；]+)；payout_edge=([+-]?[0-9.]+)",
            line,
        )
        if not match:
            return False
        self._current_price = {
            "side": match.group(1),
            "entry_price": float(match.group(2)),
            "yes_mid_price": float(match.group(3)),
            "price_source": match.group(4),
            "payout_edge": float(match.group(5)),
        }
        event["last_entry_quote"] = dict(self._current_price)
        return True

    def _parse_execution_plan(self, line):
        event = self._ensure_event()
        if not event:
            return False
        plan = re.search(
            r"【执行计划】方向=(YES|NO)；金额=\$([0-9.]+)；价格=\$([0-9.]+)；路由=([^；]+)；模式=(.+)",
            line,
        )
        if plan:
            self._pending_execution = {
                "side": plan.group(1),
                "amount_usd": float(plan.group(2)),
                "price": float(plan.group(3)),
                "route": plan.group(4),
                "mode_label": plan.group(5),
            }
            event["last_execution_plan"] = dict(self._pending_execution)
            return True

        executing = re.search(r"Executing (YES|NO) trade for \$([0-9.]+) \((SIMULATED|LIVE)\)", line)
        if executing:
            self._pending_execution = self._pending_execution or {}
            self._pending_execution.update({
                "side": executing.group(1),
                "amount_usd": float(executing.group(2)),
                "execution_mode": executing.group(3).lower(),
            })
            event["last_execution_plan"] = dict(self._pending_execution)
            return True
        return False

    def _parse_result(self, timestamp, line):
        event = self._ensure_event()
        match = re.search(r"【本轮结果】([^：]+)：([^；]+)(?:；(.+))?", line)
        if not match:
            return False
        result = {
            "timestamp": timestamp,
            "line_no": self._line_no,
            "status": match.group(1),
            "reason": match.group(2),
            "detail": match.group(3) or "",
            "event_key": self._event_key(),
        }
        self.data["results"].append(result)
        if event:
            event.setdefault("results", []).append(result)
        return True

    def _parse_trade(self, timestamp, line):
        event = self._ensure_event()
        match = re.search(r"(?:\[PAPER\] )?Bought ([0-9.]+) (YES|NO) shares @ \$([0-9.]+)", line)
        if not match:
            return False
        is_paper = "[PAPER]" in line
        amount = None
        if self._pending_execution and self._pending_execution.get("side") == match.group(2):
            amount = self._pending_execution.get("amount_usd")
        if amount is None:
            amount = self.data["config"].get("max_position_usd", 0.0)
        trade = {
            "timestamp": timestamp,
            "line_no": self._line_no,
            "mode": "paper" if is_paper else "live",
            "side": match.group(2),
            "shares": float(match.group(1)),
            "price": float(match.group(3)),
            "amount_usd": float(amount or 0.0),
            "event_key": self._event_key(),
            "question": event.get("question") if event else None,
            "window_start_utc": event.get("window_start_utc") if event else None,
            "window_end_utc": event.get("window_end_utc") if event else None,
            "market_id": event.get("market_id") if event else None,
            "execution_plan": dict(self._pending_execution) if self._pending_execution else None,
            "trade_id": event.get("trade_id") if event else None,
            "quote": dict(self._current_price) if self._current_price else None,
            "judgement": dict(self._current_judgement) if self._current_judgement else None,
        }
        self.data["trades"].append(trade)
        if event:
            event.setdefault("trades", []).append(trade)
        return True

    def _refresh_summary(self):
        result_counts = {}
        for result in self.data["results"]:
            key = f"{result['status']}:{result['reason']}"
            result_counts[key] = result_counts.get(key, 0) + 1
        total_amount = round(sum(trade.get("amount_usd", 0.0) for trade in self.data["trades"]), 6)
        self.data["summary"] = {
            "events_seen": len(self.data["events"]),
            "paper_trades": len([trade for trade in self.data["trades"] if trade.get("mode") == "paper"]),
            "total_paper_amount_usd": total_amount,
            "result_counts": result_counts,
        }


def _to_float(text):
    return float(str(text).replace(",", ""))


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()
