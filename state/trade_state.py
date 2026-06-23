"""
交易状态管理器 - TradeStateManager

功能：
1. 仅在内存中存储交易状态（无持久化）
2. 每次程序启动都是全新开始
3. 异步轮询市场结果，不阻塞主循环
4. 实时更新交易记录
"""

import json
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable


class TradePhase(Enum):
    """交易阶段"""
    PENDING = "pending"           # 待处理
    PLACING_ENTRY = "placing_entry"   # 挂单中
    WAITING_ENTRY = "waiting_entry"    # 等待成交
    HANDLING_SINGLE = "handling_single"  # 单边成交处理
    WAITING_CLOSE = "waiting_close"    # 等待强平窗口
    FORCE_CLOSING = "force_closing"    # 执行强平
    SETTLING_OUTCOME = "settling_outcome"  # 轮询市场结果
    SETTLING_BALANCE = "settling_balance"  # 等待余额稳定
    COMPLETED = "completed"        # 已完成
    FAILED = "failed"            # 失败


@dataclass
class OrderSnapshot:
    """订单快照"""
    wallet_id: str = ""
    side: str = ""
    order_id: str = ""
    status: str = "pending"
    price: float = 0.0
    shares: float = 0.0
    filled_shares: float = 0.0
    filled_amount_usd: float = 0.0
    operation: str = ""
    timestamp: str = ""


@dataclass
class TradeRecord:
    """交易记录"""
    event_id: str
    event_name: str
    condition_id: str = ""
    start_time: str = ""
    end_time: str = ""
    phase: str = TradePhase.PENDING.value

    # 钱包信息
    wallet_a_id: str = ""
    wallet_b_id: str = ""
    wallet_a_side: str = ""
    wallet_b_side: str = ""

    # 订单信息
    up_order: dict = field(default_factory=dict)
    down_order: dict = field(default_factory=dict)
    sell_order: dict = field(default_factory=dict)

    # 成交信息
    up_filled_shares: float = 0.0
    down_filled_shares: float = 0.0
    first_fill_wallet_id: str = ""

    # 结果
    outcome: str = ""  # UP, DOWN, UNKNOWN
    trigger_reason: str = ""

    # PnL
    wallet_a_pnl: float = 0.0
    wallet_b_pnl: float = 0.0
    total_pnl: float = 0.0

    # 时间戳
    created_at: str = ""
    updated_at: str = ""
    completed_at: str = ""

    # 轮询信息
    outcome_polling_started_at: str = ""
    outcome_polling_completed_at: str = ""
    balance_polling_started_at: str = ""
    balance_polling_completed_at: str = ""

    # 元数据
    metadata: dict = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict:
        """转换为字典"""
        result = asdict(self)
        return result

    @classmethod
    def from_dict(cls, data: dict) -> "TradeRecord":
        """从字典创建"""
        # 过滤掉未知字段
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)

    def update_timestamp(self):
        """更新修改时间"""
        self.updated_at = datetime.now(timezone.utc).isoformat()


class TradeStateManager:
    """
    交易状态管理器（内存模式）

    功能：
    1. 仅在内存中存储交易状态
    2. 每次启动全新开始，不加载历史状态
    3. 提供线程安全的读写操作
    """

    def __init__(self, state_file: str | Path | None = None):
        self._lock = threading.RLock()
        self._data: dict = self._empty_state()

    def _now_iso(self) -> str:
        """获取当前 UTC 时间 ISO 格式"""
        return datetime.now(timezone.utc).isoformat()

    def _empty_state(self) -> dict:
        """创建空状态"""
        return {
            "version": 1,
            "updated_at": self._now_iso(),
            "trades": {},  # event_id -> TradeRecord
            "completed_trades": [],  # 已完成的 event_id 列表（用于清理）
        }

    def _save(self):
        """内存模式：无需保存到文件"""
        pass

    def create_trade(self, event_id: str, event_name: str, **kwargs) -> TradeRecord:
        """创建新交易记录"""
        with self._lock:
            if event_id in self._data["trades"]:
                return self.get_trade(event_id)

            record = TradeRecord(
                event_id=event_id,
                event_name=event_name,
                created_at=self._now_iso(),
                updated_at=self._now_iso(),
                **kwargs
            )
            self._data["trades"][event_id] = record.to_dict()
            self._save()
            return record

    def get_trade(self, event_id: str) -> TradeRecord | None:
        """获取交易记录"""
        with self._lock:
            data = self._data["trades"].get(event_id)
            if data:
                return TradeRecord.from_dict(data)
            return None

    def update_trade(self, event_id: str, **updates) -> TradeRecord | None:
        """更新交易记录"""
        with self._lock:
            if event_id not in self._data["trades"]:
                return None

            record = TradeRecord.from_dict(self._data["trades"][event_id])
            for key, value in updates.items():
                if hasattr(record, key):
                    setattr(record, key, value)
            record.update_timestamp()

            self._data["trades"][event_id] = record.to_dict()
            self._save()
            return record

    def update_phase(self, event_id: str, phase: TradePhase, **extra) -> TradeRecord | None:
        """更新交易阶段"""
        updates = {"phase": phase.value, **extra}
        return self.update_trade(event_id, **updates)

    def mark_outcome_polling_started(self, event_id: str) -> TradeRecord | None:
        """标记开始轮询市场结果"""
        return self.update_trade(event_id, outcome_polling_started_at=self._now_iso())

    def mark_outcome_polling_completed(self, event_id: str, outcome: str) -> TradeRecord | None:
        """标记市场结果轮询完成"""
        return self.update_trade(
            event_id,
            outcome_polling_completed_at=self._now_iso(),
            outcome=outcome
        )

    def mark_completed(
        self,
        event_id: str,
        outcome: str = "",
        trigger_reason: str = "",
        wallet_a_pnl: float = 0.0,
        wallet_b_pnl: float = 0.0,
    ) -> TradeRecord | None:
        """标记交易完成"""
        total_pnl = wallet_a_pnl + wallet_b_pnl
        return self.update_trade(
            event_id,
            phase=TradePhase.COMPLETED.value,
            outcome=outcome,
            trigger_reason=trigger_reason,
            wallet_a_pnl=wallet_a_pnl,
            wallet_b_pnl=wallet_b_pnl,
            total_pnl=total_pnl,
            completed_at=self._now_iso(),
        )

    def mark_failed(self, event_id: str, error: str = "") -> TradeRecord | None:
        """标记交易失败"""
        return self.update_trade(
            event_id,
            phase=TradePhase.FAILED.value,
            error=error,
            completed_at=self._now_iso(),
        )

    def update_orders(
        self,
        event_id: str,
        up_order: dict | None = None,
        down_order: dict | None = None,
        sell_order: dict | None = None,
        up_filled_shares: float | None = None,
        down_filled_shares: float | None = None,
        first_fill_wallet_id: str = "",
    ) -> TradeRecord | None:
        """更新订单信息"""
        with self._lock:
            if event_id not in self._data["trades"]:
                return None

            record = TradeRecord.from_dict(self._data["trades"][event_id])

            if up_order is not None:
                record.up_order = up_order
            if down_order is not None:
                record.down_order = down_order
            if sell_order is not None:
                record.sell_order = sell_order
            if up_filled_shares is not None:
                record.up_filled_shares = up_filled_shares
            if down_filled_shares is not None:
                record.down_filled_shares = down_filled_shares
            if first_fill_wallet_id:
                record.first_fill_wallet_id = first_fill_wallet_id

            record.update_timestamp()
            self._data["trades"][event_id] = record.to_dict()
            self._save()
            return record

    def get_active_trades(self) -> list[TradeRecord]:
        """获取活跃交易（未完成）"""
        with self._lock:
            active = []
            for data in self._data["trades"].values():
                phase = data.get("phase", "")
                if phase not in (TradePhase.COMPLETED.value, TradePhase.FAILED.value):
                    active.append(TradeRecord.from_dict(data))
            return active

    def get_trades_by_phase(self, phase: TradePhase) -> list[TradeRecord]:
        """获取指定阶段的交易"""
        with self._lock:
            result = []
            for data in self._data["trades"].values():
                if data.get("phase") == phase.value:
                    result.append(TradeRecord.from_dict(data))
            return result

    def get_pending_outcome_polling(self) -> list[TradeRecord]:
        """获取等待轮询市场结果的交易"""
        with self._lock:
            result = []
            for data in self._data["trades"].values():
                phase = data.get("phase", "")
                # 包含 SETTLING_OUTCOME 和任何已挂单但未完成的状态
                if phase in (
                    TradePhase.SETTLING_OUTCOME.value,
                    TradePhase.WAITING_ENTRY.value,
                    TradePhase.WAITING_CLOSE.value,
                    TradePhase.HANDLING_SINGLE.value,
                    TradePhase.FORCE_CLOSING.value,
                    TradePhase.SETTLING_BALANCE.value,
                ):
                    result.append(TradeRecord.from_dict(data))
            return result

    def get_trades_for_recovery(self) -> list[TradeRecord]:
        """获取需要恢复的交易（在程序重启后）"""
        with self._lock:
            result = []
            for data in self._data["trades"].values():
                phase = data.get("phase", "")
                # 需要恢复的交易：等待轮询结果或余额
                if phase in (
                    TradePhase.SETTLING_OUTCOME.value,
                    TradePhase.SETTLING_BALANCE.value,
                ):
                    result.append(TradeRecord.from_dict(data))
            return result

    def remove_completed_trade(self, event_id: str, keep_days: int = 7):
        """移除已完成的交易（可选，保留一定天数）"""
        with self._lock:
            if event_id in self._data["trades"]:
                del self._data["trades"][event_id]
                self._save()

    def cleanup_old_completed(self, keep_days: int = 7):
        """清理旧的已完成交易"""
        with self._lock:
            cutoff = datetime.now(timezone.utc).timestamp() - (keep_days * 86400)
            to_remove = []

            for event_id, data in self._data["trades"].items():
                phase = data.get("phase", "")
                completed_at = data.get("completed_at", "")
                if phase in (TradePhase.COMPLETED.value, TradePhase.FAILED.value):
                    if completed_at:
                        try:
                            completed_time = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
                            if completed_time.timestamp() < cutoff:
                                to_remove.append(event_id)
                        except (ValueError, AttributeError):
                            pass

            for event_id in to_remove:
                del self._data["trades"][event_id]

            if to_remove:
                self._save()
                print(f"   🧹 清理了 {len(to_remove)} 条旧交易记录")

    def list_all_trades(self) -> list[TradeRecord]:
        """列出所有交易"""
        with self._lock:
            return [TradeRecord.from_dict(data) for data in self._data["trades"].values()]

    def get_summary(self) -> dict:
        """获取状态摘要"""
        with self._lock:
            phases = {}
            for data in self._data["trades"].values():
                phase = data.get("phase", "unknown")
                phases[phase] = phases.get(phase, 0) + 1

            return {
                "total_trades": len(self._data["trades"]),
                "phases": phases,
                "active_count": sum(1 for p in phases if p not in (TradePhase.COMPLETED.value, TradePhase.FAILED.value)),
                "updated_at": self._data["updated_at"],
            }


class AsyncOutcomePoller:
    """
    异步市场结果轮询器

    特点：
    1. 后台线程运行，不阻塞主循环
    2. 使用 TradeStateManager 持久化状态
    3. 支持程序重启后恢复轮询
    4. 轮询结果通过回调通知
    """

    def __init__(
        self,
        state_manager: TradeStateManager,
        poll_interval_sec: float = 5.0,
        outcome_timeout_sec: float = 900,
        on_outcome_ready: Callable[[str, str], None] | None = None,  # (event_id, outcome)
    ):
        self.state_manager = state_manager
        self.poll_interval_sec = poll_interval_sec
        self.outcome_timeout_sec = outcome_timeout_sec
        self.on_outcome_ready = on_outcome_ready

        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        # API 导入延迟到运行时，避免循环导入
        self._fetch_market_outcome: Any = None

    def _get_api(self):
        """延迟加载 API"""
        if self._fetch_market_outcome is None:
            from api import fetch_market_outcome
            self._fetch_market_outcome = fetch_market_outcome
        return self._fetch_market_outcome

    def start(self):
        """启动异步轮询线程"""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(target=self._run_loop, daemon=True, name="AsyncOutcomePoller")
            self._thread.start()
            print("   [异步轮询] 市场结果轮询器已启动")

    def stop(self):
        """停止异步轮询线程"""
        with self._lock:
            self._running = False
            if self._thread:
                self._thread.join(timeout=5)
                self._thread = None
            print("   [异步轮询] 市场结果轮询器已停止")

    def _run_loop(self):
        """轮询主循环"""
        from api import fetch_market_outcome

        while self._running:
            try:
                self._poll_all_pending()
            except Exception as e:
                print(f"   ⚠️ 异步轮询异常: {e}")

            # 等待下一次轮询
            for _ in range(int(self.poll_interval_sec * 10)):  # 检查更频繁以支持快速停止
                if not self._running:
                    break
                time.sleep(0.1)

    def _poll_all_pending(self):
        """轮询所有待处理的交易"""
        pending_trades = self.state_manager.get_pending_outcome_polling()

        for trade in pending_trades:
            if not self._running:
                break

            event_id = trade.event_id
            condition_id = trade.condition_id

            if not condition_id:
                continue

            # 检查是否超时
            if trade.outcome_polling_started_at:
                try:
                    started = datetime.fromisoformat(trade.outcome_polling_started_at.replace("Z", "+00:00"))
                    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
                    if elapsed > self.outcome_timeout_sec:
                        print(f"   [异步轮询] {trade.event_name} 轮询超时 ({elapsed:.0f}s)")
                        self.state_manager.mark_outcome_polling_completed(event_id, "TIMEOUT")
                        continue
                except (ValueError, AttributeError):
                    pass

            # 尝试获取市场结果
            try:
                result = fetch_market_outcome(condition_id, slug=None, clob_token_ids=None)

                if isinstance(result, dict):
                    outcome_raw = str(
                        result.get("outcome") or result.get("winner") or ""
                    ).upper()

                    if outcome_raw in ("UP", "YES", "DOWN", "NO"):
                        outcome = "UP" if outcome_raw in ("UP", "YES") else "DOWN"
                        print(f"   [异步轮询] {trade.event_name} 市场结果: {outcome}")

                        # 更新状态
                        self.state_manager.mark_outcome_polling_completed(event_id, outcome)

                        # 触发回调
                        if self.on_outcome_ready:
                            try:
                                self.on_outcome_ready(event_id, outcome)
                            except Exception as e:
                                print(f"   ⚠️ 回调异常: {e}")

            except Exception as e:
                # API 调用失败，继续下次轮询
                pass

    def poll_single(self, event_id: str) -> str | None:
        """
        同步轮询单个交易（用于立即检查）

        返回：outcome 字符串 或 None
        """
        trade = self.state_manager.get_trade(event_id)
        if not trade or not trade.condition_id:
            return None

        try:
            from api import fetch_market_outcome
            result = fetch_market_outcome(trade.condition_id, slug=None, clob_token_ids=None)

            if isinstance(result, dict):
                outcome_raw = str(
                    result.get("outcome") or result.get("winner") or ""
                ).upper()

                if outcome_raw in ("UP", "YES", "DOWN", "NO"):
                    outcome = "UP" if outcome_raw in ("UP", "YES") else "DOWN"
                    self.state_manager.mark_outcome_polling_completed(event_id, outcome)
                    return outcome

        except Exception:
            pass

        return None

    def check_and_resume(self) -> list[tuple[str, str]]:
        """
        检查并恢复未完成的轮询

        返回：[(event_id, outcome), ...] 已完成的列表
        """
        completed = []
        pending = self.state_manager.get_trades_for_recovery()

        for trade in pending:
            event_id = trade.event_id

            # 如果已经有结果了，触发回调
            if trade.outcome and trade.outcome not in ("UNKNOWN", ""):
                if self.on_outcome_ready:
                    try:
                        self.on_outcome_ready(event_id, trade.outcome)
                    except Exception:
                        pass
                completed.append((event_id, trade.outcome))

            # 尝试同步检查一次
            result = self.poll_single(event_id)
            if result:
                completed.append((event_id, result))

        return completed


# 全局单例
_trade_state_manager: TradeStateManager | None = None
_async_outcome_poller: AsyncOutcomePoller | None = None


def get_trade_state_manager() -> TradeStateManager:
    """获取全局交易状态管理器"""
    global _trade_state_manager
    if _trade_state_manager is None:
        _trade_state_manager = TradeStateManager()
    return _trade_state_manager


def get_async_outcome_poller() -> AsyncOutcomePoller:
    """获取全局异步轮询器"""
    global _async_outcome_poller
    if _async_outcome_poller is None:
        _async_outcome_poller = AsyncOutcomePoller(
            state_manager=get_trade_state_manager()
        )
    return _async_outcome_poller


def init_trade_state(state_file: str | Path | None = None) -> TradeStateManager:
    """初始化交易状态管理器"""
    global _trade_state_manager, _async_outcome_poller
    _trade_state_manager = TradeStateManager(state_file)
    _async_outcome_poller = AsyncOutcomePoller(state_manager=_trade_state_manager)
    return _trade_state_manager
