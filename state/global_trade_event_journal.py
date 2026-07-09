"""
Global Trade Event Journal — 跨运行时会话的持久化事件日志。

功能：
1. 所有交易事件存储在单一固定文件（不按运行时间分割）
2. 记录每次关键节点的操作信息（状态变更、订单、结算结果）
3. 支持追加写入，程序重启后可追加新事件
4. 与 StructuredRunLog 保持相同的 JSON 格式

文件位置：main/global_trade_events.json
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# 全局日志文件路径
GLOBAL_JOURNAL_FILENAME = "global_trade_events.json"
GLOBAL_JOURNAL_DIR = "main"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def cn_now_iso() -> str:
    """中国时区的当前时间（用于显示）"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class JournalEventRecord:
    """单个事件记录"""
    timestamp: str = ""              # UTC ISO 格式
    timestamp_cn: str = ""           # 中国时区
    event_type: str = ""             # 事件类型
    event_id: str = ""               # 事件ID (condition_id)
    event_name: str = ""             # 事件名称
    phase: str = ""                  # 当前阶段
    wallet: str = ""                 # 钱包名称
    wallet_id: str = ""              # 钱包ID
    side: str = ""                   # 交易方向 (UP/DOWN)
    operation: str = ""              # 操作类型 (PLACE/CANCEL/SELL/FORCE_CLOSE)
    order_id: str = ""              # 订单ID
    token_id: str = ""              # Token ID
    condition_id: str = ""           # Condition ID
    amount_usd: float = 0.0         # USD 金额
    price: float = 0.0              # 价格
    shares: float = 0.0             # 份额
    status: str = ""                # 状态
    filled_shares: float = 0.0      # 成交份额
    filled_amount_usd: float = 0.0   # 成交 USD 金额
    average_fill_price: float = 0.0  # 平均成交价
    close_price: float = 0.0        # 平仓价格
    outcome: str = ""               # 结果 (UP/DOWN)
    is_profit: bool = False         # 是否盈利
    profit_loss: float = 0.0        # 盈亏金额
    pnl_percent: float = 0.0        # 盈亏百分比
    trigger_reason: str = ""        # 触发原因
    error: str = ""                 # 错误信息
    note: str = ""                  # 备注
    raw: dict = field(default_factory=dict)  # 原始数据

    def to_dict(self) -> dict:
        return asdict(self)


class GlobalTradeEventJournal:
    """
    全局交易事件日志管理器。

    特点：
    1. 单一固定文件存储所有交易事件
    2. 线程安全，支持多线程并发写入
    3. 追加写入，程序重启后不会丢失历史数据
    4. 自动定期刷新到磁盘
    5. 保留完整的事件生命周期记录
    """

    _instance: "GlobalTradeEventJournal | None" = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs) -> "GlobalTradeEventJournal":
        """单例模式，确保全局只有一个实例"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
        self,
        journal_dir: str | Path | None = None,
        filename: str = GLOBAL_JOURNAL_FILENAME,
    ):
        """初始化全局日志管理器"""
        if hasattr(self, "_initialized") and self._initialized:
            return

        self._journal_dir = Path(journal_dir) if journal_dir else Path(GLOBAL_JOURNAL_DIR)
        self._filename = filename
        self._path = self._journal_dir / self._filename

        self._data: dict = self._load_or_create()
        self._dirty = False
        self._last_flush_monotonic = 0.0
        self._changes_since_flush = 0
        self._flush_lock = threading.Lock()
        self._initialized = True

        # 确保目录存在
        self._journal_dir.mkdir(parents=True, exist_ok=True)

    def _load_or_create(self) -> dict:
        """加载现有数据或创建新的数据结构"""
        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and "events" in data:
                        return data
            except (json.JSONDecodeError, IOError):
                pass

        # 创建新的数据结构
        return {
            "schema_version": 2,
            "journal_file": str(self._path),
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
            "total_events": 0,
            "total_orders": 0,
            "total_results": 0,
            "summary": {
                "total_trades": 0,
                "profitable_trades": 0,
                "losing_trades": 0,
                "total_pnl": 0.0,
            },
            "events": [],      # 所有事件记录
            "orders": [],      # 订单记录
            "results": [],     # 结算结果记录
        }

    def flush(self) -> None:
        """将数据刷新到磁盘"""
        with self._flush_lock:
            if not self._dirty:
                return

            self._data["updated_at"] = utc_now_iso()

            try:
                fd, tmp_name = tempfile.mkstemp(
                    prefix=f".{self._filename}.",
                    suffix=".tmp",
                    dir=str(self._journal_dir),
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        json.dump(self._data, fh, ensure_ascii=False, indent=2)
                        fh.write("\n")
                        fh.flush()
                        fh.close()
                    # 原子替换
                    os.replace(tmp_name, self._path)
                except Exception:
                    try:
                        os.unlink(tmp_name)
                    except Exception:
                        pass
                    raise
                self._dirty = False
                self._changes_since_flush = 0
                self._last_flush_monotonic = time.monotonic()
            except Exception as e:
                print(f"⚠️ Failed to flush journal: {e}")

    def _touch(self) -> None:
        """标记数据已修改，达到阈值时自动刷新"""
        self._dirty = True
        self._changes_since_flush += 1

        # 每 5 条记录或超过 10 秒强制刷新
        if self._changes_since_flush >= 5 or time.monotonic() - self._last_flush_monotonic >= 10:
            self.flush()

    def record_event(
        self,
        event_type: str,
        event_id: str = "",
        event_name: str = "",
        phase: str = "",
        wallet: str = "",
        wallet_id: str = "",
        side: str = "",
        trigger_reason: str = "",
        note: str = "",
        raw: dict | None = None,
    ) -> None:
        """记录一般事件"""
        record = JournalEventRecord(
            timestamp=utc_now_iso(),
            timestamp_cn=cn_now_iso(),
            event_type=event_type,
            event_id=event_id,
            event_name=event_name,
            phase=phase,
            wallet=wallet,
            wallet_id=wallet_id,
            side=side,
            trigger_reason=trigger_reason,
            note=note,
            raw=raw or {},
        )
        self._data["events"].append(record.to_dict())
        self._data["total_events"] += 1
        self._touch()

    def record_state_change(
        self,
        event_id: str,
        event_name: str,
        from_state: str,
        to_state: str,
        wallet: str = "",
        wallet_id: str = "",
        side: str = "",
        note: str = "",
        raw: dict | None = None,
    ) -> None:
        """记录状态变更事件"""
        self.record_event(
            event_type="state_change",
            event_id=event_id,
            event_name=event_name,
            phase=to_state,
            wallet=wallet,
            wallet_id=wallet_id,
            side=side,
            note=note,
            raw={
                "from_state": from_state,
                "to_state": to_state,
                **(raw or {}),
            },
        )

    def record_order(
        self,
        event_id: str,
        event_name: str,
        wallet: str,
        wallet_id: str,
        operation: str,
        side: str = "",
        order_id: str = "",
        token_id: str = "",
        condition_id: str = "",
        amount_usd: float = 0.0,
        price: float = 0.0,
        shares: float = 0.0,
        status: str = "",
        filled_shares: float = 0.0,
        filled_amount_usd: float = 0.0,
        average_fill_price: float = 0.0,
        close_price: float = 0.0,
        error: str = "",
        note: str = "",
        raw: dict | None = None,
    ) -> None:
        """记录订单事件"""
        record = JournalEventRecord(
            timestamp=utc_now_iso(),
            timestamp_cn=cn_now_iso(),
            event_type="order",
            event_id=event_id,
            event_name=event_name,
            phase=status,
            wallet=wallet,
            wallet_id=wallet_id,
            side=side,
            operation=operation,
            order_id=order_id,
            token_id=token_id,
            condition_id=condition_id,
            amount_usd=amount_usd,
            price=price,
            shares=shares,
            status=status,
            filled_shares=filled_shares,
            filled_amount_usd=filled_amount_usd,
            average_fill_price=average_fill_price,
            close_price=close_price,
            error=error,
            note=note,
            raw=raw or {},
        )
        self._data["orders"].append(record.to_dict())
        self._data["total_orders"] += 1
        self._touch()

    def record_result(
        self,
        event_id: str,
        event_name: str,
        outcome: str = "",
        is_profit: bool = False,
        profit_loss: float = 0.0,
        pnl_percent: float = 0.0,
        trigger_reason: str = "",
        wallet_results: dict[str, float] | None = None,
        note: str = "",
        raw: dict | None = None,
    ) -> None:
        """记录结算结果"""
        record = JournalEventRecord(
            timestamp=utc_now_iso(),
            timestamp_cn=cn_now_iso(),
            event_type="result",
            event_id=event_id,
            event_name=event_name,
            outcome=outcome,
            is_profit=is_profit,
            profit_loss=profit_loss,
            pnl_percent=pnl_percent,
            trigger_reason=trigger_reason,
            note=note,
            raw={
                "wallet_results": wallet_results or {},
                **(raw or {}),
            },
        )
        self._data["results"].append(record.to_dict())
        self._data["total_results"] += 1

        # 更新摘要
        self._data["summary"]["total_trades"] += 1
        if is_profit:
            self._data["summary"]["profitable_trades"] += 1
        else:
            self._data["summary"]["losing_trades"] += 1
        self._data["summary"]["total_pnl"] += profit_loss

        self._touch()

    def record_trade_start(
        self,
        event_id: str,
        event_name: str,
        condition_id: str,
        start_time: str,
        end_time: str,
        wallet_assignments: list[dict] | None = None,
        note: str = "",
    ) -> None:
        """记录交易开始"""
        self.record_event(
            event_type="trade_start",
            event_id=event_id,
            event_name=event_name,
            phase="PENDING",
            note=note,
            raw={
                "condition_id": condition_id,
                "start_time": start_time,
                "end_time": end_time,
                "wallet_assignments": wallet_assignments or [],
            },
        )

    def record_trade_end(
        self,
        event_id: str,
        event_name: str,
        final_state: str,
        is_profit: bool,
        total_pnl: float,
        note: str = "",
    ) -> None:
        """记录交易结束"""
        self.record_event(
            event_type="trade_end",
            event_id=event_id,
            event_name=event_name,
            phase=final_state,
            is_profit=is_profit,
            profit_loss=total_pnl,
            note=note,
        )

    def get_summary(self) -> dict:
        """获取日志摘要"""
        return {
            "journal_file": str(self._path),
            "total_events": self._data.get("total_events", 0),
            "total_orders": self._data.get("total_orders", 0),
            "total_results": self._data.get("total_results", 0),
            "summary": self._data.get("summary", {}),
            "created_at": self._data.get("created_at", ""),
            "updated_at": self._data.get("updated_at", ""),
        }

    def get_recent_events(self, limit: int = 100) -> list[dict]:
        """获取最近的事件"""
        events = self._data.get("events", [])
        return events[-limit:] if len(events) > limit else events

    def get_events_by_event_id(self, event_id: str) -> dict:
        """获取指定事件的所有记录"""
        return {
            "events": [e for e in self._data.get("events", []) if e.get("event_id") == event_id],
            "orders": [o for o in self._data.get("orders", []) if o.get("event_id") == event_id],
            "results": [r for r in self._data.get("results", []) if r.get("event_id") == event_id],
        }

    def __enter__(self) -> "GlobalTradeEventJournal":
        """支持 with 语句"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """退出时确保数据已刷新"""
        self.flush()

    @classmethod
    def get_instance(cls) -> "GlobalTradeEventJournal":
        """获取单例实例"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """重置单例（主要用于测试）"""
        with cls._lock:
            if cls._instance is not None:
                cls._instance.flush()
                cls._instance = None


# 全局便捷函数
def get_journal() -> GlobalTradeEventJournal:
    """获取全局事件日志实例"""
    return GlobalTradeEventJournal.get_instance()
