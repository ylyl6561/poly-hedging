"""
Structured JSON output for the dual-wallet event strategy.

This logger keeps the output file human-readable while also maintaining a
machine-readable sidecar for event lifecycle, wallet operations, and outcome
summary.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from strategy.dual_wallet_models import EventResultSummary, OrderSnapshot, format_operation_timestamp


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class StructuredRunLog:
    def __init__(self, run_folder, mode, log_file_name="output.log"):
        self.run_folder = Path(run_folder)
        self.path = self.run_folder / "structured_output.json"
        self.data = {
            "schema_version": 2,
            "run": {
                "mode": mode,
                "run_folder": str(self.run_folder),
                "log_file": str(self.run_folder / log_file_name),
                "started_at": None,
                "updated_at": None,
            },
            "events": [],
            "orders": [],
            "results": [],
            "summary": {
                "total_events": 0,
                "profitable_events": 0,
                "losing_events": 0,
                "halted": False,
            },
        }
        self._dirty = True
        self._last_flush_monotonic = 0.0
        self._changes_since_flush = 0

    def record_line(self, timestamp: str, line: str):
        if self.data["run"]["started_at"] is None:
            self.data["run"]["started_at"] = timestamp
        self.data["run"]["updated_at"] = timestamp
        if line:
            self._append_text_line(timestamp, line)

    def record_event(self, *, event_name: str, event_id: str, phase: str, payload: dict[str, Any] | None = None):
        row = {
            "timestamp": utc_now_iso(),
            "event_name": event_name,
            "event_id": event_id,
            "phase": phase,
            "payload": payload or {},
        }
        self.data["events"].append(row)
        self._touch()

    def record_event_state(
        self,
        *,
        event_name: str,
        event_id: str,
        flow_state: str,
        wallet_status: dict[str, Any] | None = None,
        note: str | None = None,
    ):
        payload = {
            "flow_state": flow_state,
            "wallet_status": wallet_status or {},
        }
        if note is not None:
            payload["note"] = note
        self.record_event(
            event_name=event_name,
            event_id=event_id,
            phase="state",
            payload=payload,
        )

    def record_markets(self, markets: list[dict[str, Any]]):
        self.data.setdefault("markets", [])
        self.data["markets"] = [
            {
                "question": m.get("question"),
                "slug": m.get("slug"),
                "condition_id": m.get("condition_id"),
                "end_time": m.get("end_time").isoformat() if getattr(m.get("end_time"), "isoformat", None) else m.get("end_time"),
                "is_live_now": m.get("is_live_now"),
                "source": m.get("source"),
            }
            for m in markets
        ]
        self._touch()

    def record_order(self, snapshot: OrderSnapshot):
        row = asdict(snapshot)
        row["wallet"] = snapshot.wallet.wallet_name
        row["wallet_id"] = snapshot.wallet.wallet_id
        row["role"] = snapshot.wallet.role.value
        row["timestamp_cn"] = format_operation_timestamp(snapshot.timestamp)
        row["operation"] = snapshot.operation.value
        row["side"] = snapshot.side.value
        self.data["orders"].append(row)
        self._touch()

    def record_result(self, summary: EventResultSummary):
        row = asdict(summary)
        if summary.settled_at is not None:
            row["settled_at"] = summary.settled_at.isoformat()
        self.data["results"].append(row)
        self.data["summary"]["total_events"] = len(self.data["results"])
        self.data["summary"]["profitable_events"] = sum(1 for item in self.data["results"] if item.get("is_profit"))
        self.data["summary"]["losing_events"] = sum(1 for item in self.data["results"] if not item.get("is_profit"))
        self._touch()

    def set_halted(self, halted: bool):
        self.data["summary"]["halted"] = bool(halted)
        self._touch()

    def flush(self):
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent), text=True)
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

    def _touch(self):
        self._dirty = True
        self._changes_since_flush += 1
        if self._changes_since_flush >= 10 or time.monotonic() - self._last_flush_monotonic >= 10:
            self.flush()

    def _append_text_line(self, timestamp: str, line: str):
        if "【事件结果】" in line or "【最终结果】" in line or "【钱包汇总】" in line:
            self.data.setdefault("operator_lines", []).append({"timestamp": timestamp, "line": line})
            self._touch()
