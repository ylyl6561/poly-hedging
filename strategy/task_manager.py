"""
任务管理器 (TaskManager) - 新架构的核心调度器。

整合了所有业务逻辑，支持：
1. 挂初始买单 (GTC)
2. 撤单 + 挂抛售单 (GTC)
3. 执行强平 (纯市价单)
4. 轮询市场结果
5. 轮询余额稳定

与原有的 run_event 方法完全兼容，业务逻辑保持一致。
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

# 导入 core.config 中的运行时配置
from core.config import (
    DUAL_WALLET_ENTRY_UP_PRICE,
    DUAL_WALLET_ENTRY_DOWN_PRICE,
    DUAL_WALLET_ENTRY_SHARES,
    DUAL_WALLET_MIN_SECONDS_BEFORE_START,
)

# 导入交易状态管理
from state.trade_state import (
    TradeStateManager,
    TradePhase,
    TradeRecord,
    get_trade_state_manager,
)

# 导入统一的状态归一化模块
from strategy.status import (
    normalize_clob_status,
    CLOB_FILLED_STATUSES,
    CLOB_CANCELLED_STATUSES,
)


# ===== 轮询进度跟踪 =====

class PollTracker:
    """跟踪轮询进度。"""
    _poll_count = 0
    _skipped_count = 0
    _processed_count = 0

    @classmethod
    def reset(cls) -> None:
        cls._poll_count = 0
        cls._skipped_count = 0
        cls._processed_count = 0

    @classmethod
    def start_poll(cls, new_events: int, skipped: int) -> None:
        cls._poll_count += 1
        cls._skipped_count += skipped
        print(f"\n{'='*60}")
        print(f"轮询 #{cls._poll_count} | 新事件: {new_events} | 已跳过: {skipped}")
        print(f"{'='*60}")

    @classmethod
    def start_event(cls, name: str, start_seconds: float, end_time: str) -> None:
        cls._processed_count += 1
        print(f"\n>> 处理事件: {name}")
        print(f"   距离开始: {start_seconds:.0f}s | 结束时间: {end_time} UTC")

    @classmethod
    def end_poll(cls) -> None:
        print(f"\n--- 轮询 #{cls._poll_count} 完成 ---")
        print("所有事件已处理，等待下一批...")


# ===== 人性化日志系统 =====

class TradeLogger:
    """人性化交易日志工具 - 终端进度风格。"""

    UP_SYMBOL = "▲"
    DOWN_SYMBOL = "▼"

    @classmethod
    def event_info(cls, name: str, start_seconds: float, end_time: str,
                   wallet_a_side: str, wallet_b_side: str,
                   amount: float, up_price: float, down_price: float,
                   wallet_a_balance: float) -> None:
        """打印事件基本信息（用于开始处理时）。"""
        wallet_a_symbol = cls.UP_SYMBOL if wallet_a_side == "UP" else cls.DOWN_SYMBOL
        wallet_b_symbol = cls.UP_SYMBOL if wallet_b_side == "UP" else cls.DOWN_SYMBOL
        print(f"   分配: Wallet A{wallet_a_symbol} | Wallet B{wallet_b_symbol} | 金额: ${amount:.2f} | UP: {up_price:.2f} / DOWN: {down_price:.2f}")
        print(f"   余额: Wallet A={wallet_a_balance:.2f} USDC")

    @classmethod
    def placing_orders(cls) -> None:
        """开始挂单。"""
        print("   [挂单] 提交初始买单...")

    @classmethod
    def order_submitted(cls, wallet_name: str, side: str, price: float) -> None:
        """订单提交。"""
        symbol = cls.UP_SYMBOL if side == "UP" else cls.DOWN_SYMBOL
        print(f"      ... {wallet_name} {symbol} @ {price:.2f}")

    @classmethod
    def order_failed(cls, wallet_name: str, error: str) -> None:
        """订单失败。"""
        print(f"      ❌ {wallet_name}: {error}")

    @classmethod
    def single_filled(cls, wallet_name: str, side: str, shares: float) -> None:
        """单边成交。"""
        symbol = cls.UP_SYMBOL if side == "UP" else cls.DOWN_SYMBOL
        print(f"\n   [单边成交] {wallet_name} {symbol} | 份额: {shares:.2f}")
        print(f"   策略: 保留未成交侧 + 挂抛售单")

    @classmethod
    def both_filled(cls) -> None:
        """双边成交。"""
        print(f"\n   [双边成交] 两边都已成交")

    @classmethod
    def sell_order_placed(cls, wallet_name: str, side: str, price: float, shares: float) -> None:
        """抛售单已挂。"""
        symbol = cls.UP_SYMBOL if side == "UP" else cls.DOWN_SYMBOL
        print(f"   [抛售] {wallet_name} {symbol} @ {price:.2f} | 份额: {shares:.2f}")

    @classmethod
    def sell_order_failed(cls, wallet_name: str, side: str, price: float, shares: float, error: str) -> None:
        """抛售单挂单失败。"""
        symbol = cls.UP_SYMBOL if side == "UP" else cls.DOWN_SYMBOL
        print(f"   [抛售失败] {wallet_name} {symbol} @ {price:.2f} | 份额: {shares:.2f} | 原因: {error}")

    @classmethod
    def waiting_close_window(cls, remaining: float) -> None:
        """等待强平窗口。"""
        print(f"\n   [强平窗口] 剩余 {remaining:.0f}s...")

    @classmethod
    def close_window_reached(cls) -> None:
        """强平窗口到达。"""
        print(f"   [强平窗口] 到达，开始执行强平...")

    @classmethod
    def force_close(
        cls, event_name: str, wallet_name: str, side: str, amount_str: str, result: str,
        filled_shares: float | None = None, fill_price: float | None = None,
        filled_amount_usd: float | None = None,
    ) -> None:
        """强平执行。"""
        symbol = cls.UP_SYMBOL if side == "UP" else cls.DOWN_SYMBOL
        status = "✓" if result == "成功" else "✗"

        # 补充成交详情
        extra = []
        if filled_shares is not None:
            extra.append(f"{filled_shares:.4f}份")
        if fill_price is not None:
            extra.append(f"@ {fill_price:.4f}")
        if filled_amount_usd is not None:
            extra.append(f"≈ ${filled_amount_usd:.2f}")

        detail = f" [{', '.join(extra)}]" if extra else ""
        print(f"   [强平] {event_name} | {wallet_name} {symbol} | {amount_str} {status}{detail}")

    @classmethod
    def order_cancelled(cls, wallet_name: str, reason: str = "") -> None:
        """撤单。"""
        reason_text = f" ({reason})" if reason else ""
        print(f"   [撤单] {wallet_name}{reason_text}")

    @classmethod
    def waiting_result(cls, timeout: int) -> None:
        """等待结果。"""
        print(f"   [结算] 等待市场结果... (超时 {timeout}s)")

    @classmethod
    def poll_progress(cls, name: str, rounds: str, remaining: str) -> None:
        """轮询进度。"""
        print(f"   [轮询] {name} | 轮次: {rounds} | 剩余: {remaining}s")

    @classmethod
    def outcome_revealed(cls, outcome: str) -> None:
        """结果揭晓。"""
        symbol = cls.UP_SYMBOL if outcome == "YES" else cls.DOWN_SYMBOL
        print(f"   [结果揭晓] {symbol} {outcome}")

    @classmethod
    def waiting_balance(cls, stable_rounds: int) -> None:
        """等待余额稳定。"""
        print(f"   [结算] 等待余额稳定... (需 {stable_rounds} 轮确认)")

    @classmethod
    def balance_stable(cls) -> None:
        """余额已稳定。"""
        print(f"   [结算完成] 余额已稳定")

    @classmethod
    def event_result(cls, total_pnl: float, is_profit: bool, wallet_results: dict) -> None:
        """打印事件结果。"""
        emoji = "+" if is_profit else "-"
        status = "盈利" if is_profit else "亏损"
        print(f"\n   PnL: {emoji}${abs(total_pnl):.4f} ({status})")
        for wallet_id, pnl in wallet_results.items():
            pnl_sign = "+" if pnl >= 0 else "-"
            print(f"      {wallet_id}: {pnl_sign}${abs(pnl):.4f}")

    @classmethod
    def event_skip(cls, reason: str) -> None:
        """跳过事件。"""
        print(f"   [跳过] {reason}")

    @classmethod
    def halt(cls, reason: str) -> None:
        """停机。"""
        print(f"\n⚠️ 停机: {reason}")

    @classmethod
    def manager_start(cls, active_count: str) -> None:
        """管理器启动。"""
        print(f"\n🟢 任务管理器启动 | 活跃任务: {active_count}")

    @classmethod
    def manager_complete(cls, completed: str, remaining: str) -> None:
        """管理器完成。"""
        print(f"   ✅ 完成 {completed} 个任务 | 剩余活跃: {remaining}")

    @classmethod
    def manager_stop(cls, completed: str, failed: str) -> None:
        """管理器停止。"""
        print(f"\n🔴 任务管理器停止 | 完成: {completed} | 失败: {failed}")

    @classmethod
    def waiting(cls, state: str, remaining: float = None) -> None:
        """等待中状态的简短日志。"""
        if remaining is not None:
            print(f"   → {state} ({remaining:.0f}s)")
        else:
            print(f"   → {state}")

    @classmethod
    def progress(cls, from_state: str, to_state: str) -> None:
        """状态转换日志。"""
        print(f"   {from_state} → {to_state}")

from api import fetch_token_balance, fetch_order_status, get_wallet_usdc_balance
from notifications.feishu_notifier import (
    send_force_close_notification,
    send_cancel_order_notification,
    send_close_window_notification,
)
from notifications.feishu_tools import send_feishu
from state.reconcile_export import export_dual_wallet_event_to_excel

from strategy.event_task import EventTask, coalesce_filled_shares
from strategy.event_task_state import (
    EventTaskState,
    is_active_state,
    is_terminal_state,
    get_state_display_name,
    get_state_priority,
)
from strategy.pollers import (
    Poller,
    OutcomePoller,
    BalanceStabilityPoller,
    create_outcome_poller,
    create_balance_poller,
)
from strategy.order_executor_v2 import ExecutionOutcome, OrderExecutorV2, OrderOperationResult
from strategy.dual_wallet_models import (
    DualWalletEventState,
    EventOutcome,
    EventFlowState,
    LossWindowTracker,
    OrderSnapshot,
    OrderStatus,
    OrderSide,
    OperationType,
    WalletIdentity,
)


# 类型别名
OrderExecutorProtocol = Any  # 避免循环导入


@dataclass
class TaskManagerConfig:
    """任务管理器配置。"""
    # 轮询间隔
    poll_interval_sec: float = 0.6
    entry_timeout_sec: int = 92  # 从 core.config.DUAL_WALLET_ENTRY_TIMEOUT_SEC 读取
    force_close_window_sec: int = 88  # 从 core.config.DUAL_WALLET_FORCE_CLOSE_WINDOW_SEC 读取
    fixed_sell_price: float = 0.6  # 从 core.config.DUAL_WALLET_FIXED_SELL_PRICE 读取
    market_close_price: float = 0.99  # 从 core.config.DUAL_WALLET_MARKET_CLOSE_PRICE 读取
    max_consecutive_losses: int = 2
    min_seconds_before_start: int = 15  # 从 core.config.DUAL_WALLET_MIN_SECONDS_BEFORE_START 读取

    # 结算配置
    outcome_poll_timeout_sec: int = 900
    outcome_poll_interval_sec: int = 5
    settlement_poll_timeout_sec: int = 180
    settlement_poll_interval_sec: int = 20
    settlement_stable_rounds: int = 3

    # 日志配置
    progress_log_interval_sec: float = 30.0
    enable_feishu: bool = True

    @classmethod
    def from_config_dict(cls, config: dict) -> "TaskManagerConfig":
        """从配置字典创建。"""
        return cls(
            poll_interval_sec=float(config.get("dual_wallet_poll_interval_sec", 1)),
            entry_timeout_sec=int(config.get("dual_wallet_entry_timeout_sec", 92)),
            force_close_window_sec=int(config.get("dual_wallet_force_close_window_sec", 88)),
            fixed_sell_price=float(config.get("dual_wallet_fixed_sell_price", 0.6)),
            market_close_price=float(config.get("dual_wallet_fak_close_price", 0.99)),
            max_consecutive_losses=int(config.get("dual_wallet_max_consecutive_losses", 2)),
            min_seconds_before_start=int(config.get("dual_wallet_min_seconds_before_start", 15)),
            outcome_poll_timeout_sec=int(config.get("dual_wallet_outcome_poll_timeout_sec", 900)),
            outcome_poll_interval_sec=int(config.get("dual_wallet_outcome_poll_interval_sec", 5)),
            settlement_poll_timeout_sec=int(config.get("dual_wallet_settlement_poll_timeout_sec", 180)),
            settlement_poll_interval_sec=int(config.get("dual_wallet_settlement_poll_interval_sec", 20)),
            settlement_stable_rounds=int(config.get("dual_wallet_settlement_stable_rounds", 3)),
            progress_log_interval_sec=float(config.get("dual_wallet_progress_log_interval_sec", 30.0)),
            enable_feishu=bool(config.get("dual_wallet_enable_feishu", True)),
        )


class TaskManager:
    """
    任务管理器 - 新架构的核心。

    管理多个 EventTask 的生命周期，提供非阻塞的批量更新。
    主循环只需调用 tick() 即可更新所有任务。
    
    业务逻辑与原有 run_event 完全兼容。
    """

    def __init__(
        self,
        config: TaskManagerConfig,
        executor: OrderExecutorProtocol,
        wallets: list[WalletIdentity],
        run_folder: str,
        dry_run: bool = False,
        structured_log: Any = None,
        state_manager: TradeStateManager | None = None,
    ):
        self.config = config
        self.executor = executor
        self.wallets = wallets
        self.run_folder = run_folder
        self.dry_run = dry_run
        self.structured_log = structured_log
        self._is_paper = dry_run  # 用于飞书通知判断

        # 交易状态管理器（用于持久化和异步轮询）
        self._state_manager = state_manager or get_trade_state_manager()

        # 订单执行器 V2
        self._order_exec = OrderExecutorV2(
            executor=executor,
            fixed_sell_price=config.fixed_sell_price,
            dry_run=dry_run,
        )

        # 任务存储
        self._tasks: dict[str, EventTask] = {}
        self._completed_tasks: list[EventTask] = []
        self._failed_tasks: list[EventTask] = []

        # 轮询器存储（用于同步轮询）
        self._pollers: dict[str, Poller] = {}

        # 损失窗口跟踪
        self._loss_window = LossWindowTracker()
        self._halted = False
        self._halt_reason: str | None = None

        # 事件开始时的余额
        self._start_balances: dict[str, float | None] = {}

        # 标记是否使用异步轮询
        self._async_polling_enabled = True

    # ===== 任务管理 =====

    def add_task(self, task: EventTask) -> EventTask:
        """添加新任务。"""
        self._tasks[task.event_id] = task

        # 创建交易记录
        wallet_assignments = {}
        for wallet in task.wallets:
            side = task.side_by_wallet_id.get(wallet.wallet_id)
            wallet_assignments[wallet.wallet_id] = side.value if side else ""

        self._state_manager.create_trade(
            event_id=task.event_id,
            event_name=task.event_name,
            condition_id=task.condition_id,
            start_time=task.start_time.isoformat() if task.start_time else "",
            end_time=task.end_time.isoformat() if task.end_time else "",
            wallet_a_id=self.wallets[0].wallet_id if len(self.wallets) > 0 else "",
            wallet_b_id=self.wallets[1].wallet_id if len(self.wallets) > 1 else "",
            wallet_a_side=wallet_assignments.get(self.wallets[0].wallet_id, "") if len(self.wallets) > 0 else "",
            wallet_b_side=wallet_assignments.get(self.wallets[1].wallet_id, "") if len(self.wallets) > 1 else "",
        )

        # 记录开始余额
        for wallet in task.wallets:
            if wallet.wallet_id not in self._start_balances:
                try:
                    payload = get_wallet_usdc_balance(account=wallet.account)
                    balance = payload.get("balance_usdc") if isinstance(payload, dict) else None
                    self._start_balances[wallet.wallet_id] = float(balance) if balance else None
                except Exception:
                    self._start_balances[wallet.wallet_id] = None

        # 设置回调
        task.on_state_change = self._handle_state_change
        task.on_progress = self._handle_task_progress
        task.on_complete = self._handle_task_complete

        return task

    def remove_task(self, event_id: str) -> EventTask | None:
        """移除任务。"""
        return self._tasks.pop(event_id, None)

    def get_task(self, event_id: str) -> EventTask | None:
        """获取任务。"""
        return self._tasks.get(event_id)

    # ===== 辅助方法（从原有 run_event 迁移） =====

    def _announce_event(self, task: EventTask, condition_id: str, amount_usd: float, up_price: float, down_price: float) -> None:
        """打印事件公告（迁移自原有 _announce_event）。"""
        end_time_str = task.end_time.astimezone(timezone.utc).strftime("%m-%d %H:%M:%S")
        selected = [f"{wallet.wallet_name}" for wallet in self.wallets]
        mapping = []
        for wallet in self.wallets:
            side = task.side_by_wallet_id.get(wallet.wallet_id)
            mapping.append(f"{wallet.wallet_name}→{side.value if side else '?'}")
        print(f"   📋 账号分配: {' | '.join(mapping)}")

    def _announce_event_start(self, task: EventTask, start_seconds: float, amount_usd: float, up_price: float, down_price: float) -> None:
        """在开始处理事件时打印事件信息。"""
        end_time_str = task.end_time.astimezone(timezone.utc).strftime("%H:%M:%S")

        # 获取钱包分配
        wallet_a_side = "UP"
        wallet_b_side = "DOWN"
        if len(self.wallets) >= 2:
            for wallet in self.wallets:
                side = task.side_by_wallet_id.get(wallet.wallet_id)
                if wallet.wallet_id == self.wallets[0].wallet_id:
                    wallet_a_side = side.value if side else "?"
                else:
                    wallet_b_side = side.value if side else "?"

        TradeLogger.event_info(
            name=task.event_name,
            start_seconds=start_seconds,
            end_time=end_time_str,
            wallet_a_side=wallet_a_side,
            wallet_b_side=wallet_b_side,
            amount=amount_usd,
            up_price=up_price,
            down_price=down_price,
            wallet_a_balance=0.0,  # 不再阻塞获取余额
        )

    def _log_wallet_account_data(self, amount_usd: float) -> None:
        """打印账户资金日志。"""
        import core.config as config_module
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        for wallet in self.wallets:
            try:
                payload = get_wallet_usdc_balance(account=wallet.account)
                if isinstance(payload, dict) and payload.get("success"):
                    balance = payload.get("balance_usdc", 0)
                    available = float(balance) if balance is not None else 0
                    print(f"[{now_str}] 【账户资金】{wallet.wallet_name}：available_usdc={available:.4f} | next_order_amount={amount_usd:.4f}")
                else:
                    error = payload.get("error") if isinstance(payload, dict) else "balance_unavailable"
                    print(f"[{now_str}] 【账户资金】{wallet.wallet_name}：error={error}")
            except Exception as e:
                print(f"[{now_str}] 【账户资金】{wallet.wallet_name}：error={e}")

    def _ensure_wallet_capacity(self, task: EventTask, amount_usd: float) -> bool:
        """
        检查钱包余额是否足够（迁移自原有 _ensure_wallet_capacity）。

        Returns:
            True 表示容量足够，False 表示容量不足
        """
        insufficient_wallets: list[str] = []
        for wallet in self.wallets:
            try:
                payload = get_wallet_usdc_balance(account=wallet.account)
                balance = payload.get("balance_usdc") if isinstance(payload, dict) else None
                if not isinstance(payload, dict) or not payload.get("success") or balance is None:
                    insufficient_wallets.append(f"{wallet.wallet_name}:balance_unavailable")
                    continue
                if float(balance) + 1e-9 < float(amount_usd):
                    insufficient_wallets.append(f"{wallet.wallet_name}:need={float(amount_usd):.4f},available={float(balance):.4f}")
            except Exception:
                insufficient_wallets.append(f"{wallet.wallet_name}:invalid_balance")

        if insufficient_wallets:
            self._halt_reason = f"余额不足 | {'; '.join(insufficient_wallets)}"
            if not self.dry_run:
                self._halted = True
                TradeLogger.halt(self._halt_reason)
            else:
                TradeLogger.event_skip(self._halt_reason)
            return False
        return True

    def _log_state(self, task: EventTask, phase: str, note: str | None = None, payload: dict[str, Any] | None = None) -> None:
        """打印状态日志（迁移自原有 _log_state）。"""
        if not self.structured_log:
            return
        extra_payload = payload or {}
        self.structured_log.record_event_state(
            event_name=task.event_name,
            event_id=task.event_id,
            flow_state=phase,
            wallet_status={wallet_id: snapshot.status for wallet_id, snapshot in task.current_orders.items()},
            note=note,
            payload={
                "selected_accounts": self._build_selected_account_payload(),
                "side_assignment": self._build_side_assignment_payload(task),
                **extra_payload,
            },
        )

    def _build_selected_account_payload(self) -> list[dict[str, str]]:
        """构建选中账号的 payload。"""
        return [
            {
                "account_id": account.account_id,
                "label": account.label,
                "wallet_address": account.wallet_address,
            }
            for account in [wallet.account for wallet in self.wallets]
        ]

    def _build_side_assignment_payload(self, task: EventTask) -> list[dict[str, str]]:
        """构建方向分配的 payload。"""
        payload: list[dict[str, str]] = []
        for wallet in self.wallets:
            side = task.side_by_wallet_id.get(wallet.wallet_id)
            payload.append(
                {
                    "account_id": wallet.account.account_id,
                    "label": wallet.account.label,
                    "wallet_address": wallet.account.wallet_address,
                    "role": wallet.role.value,
                    "side": side.value if side else "UNKNOWN",
                }
            )
        return payload

    @property
    def tasks(self) -> list[EventTask]:
        """获取所有任务（按优先级排序）。"""
        return sorted(
            self._tasks.values(),
            key=lambda t: get_state_priority(t.state),
        )

    @property
    def active_tasks(self) -> list[EventTask]:
        """获取活跃任务。"""
        return [t for t in self._tasks.values() if t.is_active]

    @property
    def completed_tasks(self) -> list[EventTask]:
        """获取已完成任务（包括成功和失败）。"""
        return list(self._completed_tasks) + list(self._failed_tasks)

    @property
    def failed_tasks(self) -> list[EventTask]:
        """获取失败任务。"""
        return list(self._failed_tasks)

    @property
    def active_count(self) -> int:
        """活跃任务数量。"""
        return len(self.active_tasks)

    # ===== 主循环 =====

    def tick(self) -> list[EventTask]:
        """
        驱动所有任务前进（非阻塞）。

        返回刚完成的任务列表。
        """
        just_completed: list[EventTask] = []

        # 每 10 秒打印一次活跃 task 数，用于诊断 API 调用频率
        if not hasattr(self, "_last_task_count_log"):
            self._last_task_count_log = 0.0
        now = time.monotonic()
        if now - self._last_task_count_log >= 10.0:
            self._last_task_count_log = now
            print(f"   [活跃任务] {len(self.active_tasks)} 个 task")

        # 遍历所有活跃任务
        for task in list(self.active_tasks):
            # 根据当前状态执行对应的处理逻辑
            self._process_state(task)

            # 检查是否刚完成
            if task.is_terminal and task not in just_completed:
                just_completed.append(task)

        # 处理刚完成的任务
        for task in just_completed:
            self._finalize_task(task)

        # 清理已终止的任务
        self._cleanup_terminal_tasks()

        return just_completed

    # ===== 状态处理 =====

    def _process_state(self, task: EventTask) -> None:
        """根据当前状态处理任务。"""
        state = task.state

        if state == EventTaskState.PENDING:
            self._process_pending(task)
        elif state == EventTaskState.PLACING_ENTRY:
            self._process_placing_entry(task)
        elif state == EventTaskState.WAITING_ENTRY:
            self._process_waiting_entry(task)
        elif state == EventTaskState.HANDLING_SINGLE:
            self._process_handling_single(task)
        elif state == EventTaskState.WAITING_CLOSE_WINDOW:
            self._process_waiting_close_window(task)
        elif state == EventTaskState.FORCE_CLOSING:
            self._process_force_closing(task)
        elif state == EventTaskState.SETTLING_OUTCOME:
            self._process_settling_outcome(task)
        elif state == EventTaskState.SETTLING_BALANCE:
            self._process_settling_balance(task)

    def _process_pending(self, task: EventTask) -> None:
        """处理 PENDING 状态。"""
        print(f"   [状态] PENDING - {task.event_name}")

        # 规范化时间
        end_time_utc = task.end_time
        if end_time_utc.tzinfo is None:
            end_time_utc = end_time_utc.astimezone(timezone.utc)
            task.end_time = end_time_utc

        start_time_utc = task.start_time
        if start_time_utc and start_time_utc.tzinfo is None:
            start_time_utc = start_time_utc.astimezone(timezone.utc)
            task.start_time = start_time_utc

        now = datetime.now(timezone.utc)
        time_to_event_end = (end_time_utc - now).total_seconds()
        time_to_event_start = (start_time_utc - now).total_seconds() if start_time_utc else 0

        # 检查最小提前量
        if time_to_event_start > 0 and time_to_event_start < self.config.min_seconds_before_start:
            print(f"   [跳过] 时间不足: 距开始 {time_to_event_start:.0f}s < {self.config.min_seconds_before_start}s")
            task.transition_to(EventTaskState.SKIPPED, f"时间不足")
            return

        # 事件已结束，直接进入结算
        if time_to_event_end <= 0:
            print(f"   [结束] 事件已结束")
            task.trigger_reason = "event_already_ended"
            task.trigger_detail = f"remaining_to_end_sec={int(time_to_event_end)};event_ended_before_wait"
            task.transition_to(EventTaskState.SETTLING_OUTCOME, "事件已结束")
            return

        # 5m 市场：start_time == end_time，只要距结束时间足够就立即挂单
        # 不再要求等待 start_time
        print(f"[执行] {task.event_name}")
        print(f"   开始: {start_time_utc.strftime('%H:%M:%S')} UTC ({time_to_event_start:.0f}s) | 结束: {end_time_utc.strftime('%H:%M:%S')} UTC")

        # 从 core.config 读取配置值
        entry_shares = DUAL_WALLET_ENTRY_SHARES
        up_price = DUAL_WALLET_ENTRY_UP_PRICE
        down_price = DUAL_WALLET_ENTRY_DOWN_PRICE
        condition_id = task.condition_id

        # 打印任务管理器启动
        print(f"【任务管理器】启动 | 活跃任务: 1")

        # 打印钱包分配
        wallet_assignments = []
        for wallet in self.wallets:
            side = task.side_by_wallet_id.get(wallet.wallet_id)
            side_symbol = "▲" if side == OrderSide.UP else "▼" if side == OrderSide.DOWN else "?"
            wallet_assignments.append(f"{wallet.wallet_name}{side_symbol}")
        avg_price = (up_price + down_price) / 2.0 if (up_price + down_price) > 0 else 0.0
        approx_amount = entry_shares * avg_price
        print(
            f"   分配: {' | '.join(wallet_assignments)} | "
            f"数量/单: {entry_shares:.4f} shares | "
            f"预计金额: ${approx_amount:.2f} (均价 ${avg_price:.2f}) | "
            f"UP: {up_price:.2f} / DOWN: {down_price:.2f}"
        )

        # 打印余额
        print("   [余额] 正在获取钱包余额...")
        self._log_wallet_account_data(approx_amount)

        # 进入挂单阶段，状态机会在下一 tick 调用 _process_placing_entry
        task.metadata["entry_shares"] = entry_shares
        task.metadata["up_price"] = up_price
        task.metadata["down_price"] = down_price
        task.metadata["fee_rate_bps"] = task.metadata.get("fee_rate_bps", 0)
        task.transition_to(EventTaskState.PLACING_ENTRY, "前置检查完成，开始挂单")

    def _process_placing_entry(self, task: EventTask) -> None:
        """处理 PLACING_ENTRY 状态 - 挂初始买单。"""
        print("   [挂单] 提交初始买单...")

        up_price = task.metadata.get("up_price", 0.5)
        down_price = task.metadata.get("down_price", 0.5)
        entry_shares = float(task.metadata.get("entry_shares", task.metadata.get("amount_usd", 10.0)))
        fee_rate_bps = task.metadata.get("fee_rate_bps", 0)

        for wallet in task.wallets:
            side = task.side_by_wallet_id.get(wallet.wallet_id)
            price = up_price if side == OrderSide.UP else down_price
            # 按"每单固定 token 数量"换算成 amount_usd（保持原有资金门槛判断逻辑不变）
            amount_usd = entry_shares * float(price) if price and price > 0 else 0.0

            result = self._order_exec.place_entry_order(
                wallet=wallet,
                event_name=task.event_name,
                side=side,
                amount_usd=amount_usd,
                price=price,
                clob_token_ids=task.clob_token_ids,
                fee_rate_bps=fee_rate_bps,
                condition_id=task.condition_id,
            )

            # 记录订单快照（snapshot 同步每单数量，便于后续 sell/force_close 还原）
            if result.snapshot:
                if getattr(result.snapshot, "shares", None) in (None, 0.0):
                    result.snapshot.shares = entry_shares
                if not getattr(result.snapshot, "amount_usd", None):
                    result.snapshot.amount_usd = amount_usd
                task.mark_order(result.snapshot)

            # 打印日志
            self._log_order_submission(wallet, side, amount_usd, result.snapshot)

            # 如果失败，打印详细错误
            if not result.outcome.success:
                print(f"      ⚠️ 挂单失败: {result.outcome.error}")

        # 同步把 entry_shares 写到 metadata 顶层，下游可直接读取
        task.metadata["entry_shares"] = entry_shares
        task.trigger_reason = "entry_placed"
        task.transition_to(EventTaskState.WAITING_ENTRY, "挂单完成")
        print("   [挂单完成] 等待成交...")

    def _process_waiting_entry(self, task: EventTask) -> None:
        """处理 WAITING_ENTRY 状态 - 等待成交。"""
        # 刷新订单状态
        self._refresh_order_statuses(task)

        # 规范化时间
        end_time_utc = task.end_time
        if end_time_utc.tzinfo is None:
            end_time_utc = end_time_utc.astimezone(timezone.utc)

        start_time_utc = task.start_time
        if start_time_utc and start_time_utc.tzinfo is None:
            start_time_utc = start_time_utc.astimezone(timezone.utc)

        close_window_sec = task.close_window_sec
        deadline = start_time_utc + timedelta(seconds=self.config.entry_timeout_sec) if start_time_utc else None
        now = datetime.now(timezone.utc)

        # 检查双边是否都已成交
        both_filled, up_shares, down_shares = task.check_both_sides_filled()
        if both_filled:
            task.up_filled_shares = up_shares
            task.down_filled_shares = down_shares
            task.trigger_reason = "both_sides_filled"
            task.transition_to(EventTaskState.SETTLING_OUTCOME, "双边成交确认")
            print("   [状态转移] WAITING_ENTRY → SETTLING_OUTCOME (双边成交)")
            return

        # 检查单边成交
        single_filled, filled_wallet_id, shares = task.check_single_side_filled()
        if single_filled:
            task.first_fill_wallet_id = filled_wallet_id
            task.trigger_reason = "single_side_filled"
            task.transition_to(EventTaskState.HANDLING_SINGLE, f"单边成交: {filled_wallet_id}")
            print(f"   [状态转移] WAITING_ENTRY → HANDLING_SINGLE (单边成交: {filled_wallet_id})")
            return

        # 检查 deadline 是否到达
        if deadline and now >= deadline:
            remaining_to_end = (end_time_utc - now).total_seconds()
            if remaining_to_end <= close_window_sec:
                task.trigger_reason = "force_close_window"
                print(f"   [超时进入] 强平窗口 (deadline={deadline.strftime('%H:%M:%S')}, now={now.strftime('%H:%M:%S')})")
                task.transition_to(EventTaskState.WAITING_CLOSE_WINDOW, "进入强平窗口")
            else:
                # entry_timeout 触发，双边都没成交，取消双边挂单
                task.trigger_reason = "entry_timeout"
                self._cancel_both_entry_orders(task)
                print(f"   [超时] 等待成交超时，取消双边挂单，进入结算 (deadline={deadline.strftime('%H:%M:%S')}, now={now.strftime('%H:%M:%S')})")
                task.transition_to(EventTaskState.SETTLING_OUTCOME, "等待超时")
                print(f"   [状态转移] WAITING_ENTRY → SETTLING_OUTCOME (entry_timeout)")
            return

        # 检查是否进入强平窗口
        remaining_to_end = (end_time_utc - now).total_seconds()
        if remaining_to_end <= close_window_sec:
            task.trigger_reason = "force_close_window"
            print(f"   [进入] 强平窗口 (remaining={remaining_to_end:.0f}s <= {close_window_sec}s)")
            task.transition_to(EventTaskState.WAITING_CLOSE_WINDOW, "进入强平窗口")
            print(f"   [状态转移] WAITING_ENTRY → WAITING_CLOSE_WINDOW (强平窗口)")
            return

        # 每隔一段时间打印等待状态
        if hasattr(task, '_last_wait_log'):
            log_interval = 30  # 每 30 秒打印一次
            if now.timestamp() - task._last_wait_log > log_interval:
                print(f"   [等待] 成交中... (剩余 {remaining_to_end:.0f}s)")
                task._last_wait_log = now.timestamp()
        else:
            task._last_wait_log = now.timestamp()

    @staticmethod
    def _parse_clob_balance_error(error_text: str) -> dict[str, float | str | None]:
        """从 CLOB 错误消息中提取 balance / order amount / token_id 等关键字段。

        形如:
          "PolyApiException[status_code=400, error_message={'error':
            'not enough balance / allowance: the balance is not enough ->
            balance: 0, order amount: 5000000'}]"
        """
        if not error_text:
            return {}
        out: dict[str, float | str | None] = {}

        def _num(pattern: str) -> float | None:
            m = re.search(pattern, error_text)
            if not m:
                return None
            try:
                return float(m.group(1))
            except (ValueError, IndexError):
                return None

        balance = _num(r"balance:\s*([0-9]+(?:\.[0-9]+)?)")
        order_amount = _num(r"order amount:\s*([0-9]+(?:\.[0-9]+)?)")
        if balance is not None:
            out["balance_raw"] = balance
            out["balance_shares"] = balance / 1_000_000.0
        if order_amount is not None:
            out["order_amount_raw"] = order_amount
            out["order_amount_shares"] = order_amount / 1_000_000.0

        m = re.search(r"status_code\s*[=:]\s*([0-9]+)", error_text)
        if m:
            out["status_code"] = int(m.group(1))
        return out

    def _diagnose_sell_balance_mismatch(
        self,
        task: "EventTask",
        wallet: "WalletIdentity",
        side: "OrderSide | None",
        shares_attempted: float,
        price_attempted: float,
        error_text: str,
    ) -> None:
        """抛售单挂单失败时打印本地 vs CLOB 两侧的对账信息，定位 root cause。"""
        side_str = side.value if side else "?"
        side_token_index = side.token_index if side else 0
        task_token_id = None
        if task.clob_token_ids and 0 <= side_token_index < len(task.clob_token_ids):
            task_token_id = task.clob_token_ids[side_token_index]

        parsed = self._parse_clob_balance_error(error_text)

        print(
            f"   [抛售失败·诊断] [{wallet.wallet_name}] {side_str} 侧 sell attempt failed\n"
            f"      task.clob_token_ids[{side_token_index}] = {task_token_id}\n"
            f"      task.clob_token_ids = {task.clob_token_ids}\n"
            f"      shares_attempted = {shares_attempted:.4f}\n"
            f"      price_attempted = {price_attempted:.4f}\n"
            f"      raw_shares_attempted (CLOB decimals) = {int(round(shares_attempted * 1_000_000))}"
        )
        if parsed:
            balance_shares = parsed.get("balance_shares")
            order_amount_shares = parsed.get("order_amount_shares")
            balance_raw = parsed.get("balance_raw")
            order_amount_raw = parsed.get("order_amount_raw")
            status_code = parsed.get("status_code")
            print(
                f"      CLOB raw error: status_code={status_code} | "
                f"balance_raw={balance_raw} ({balance_shares} shares) | "
                f"order_amount_raw={order_amount_raw} ({order_amount_shares} shares)"
            )
            if isinstance(balance_shares, (int, float)) and shares_attempted > 0:
                if balance_shares == 0:
                    print(
                        "      >>> DIAGNOSIS: CLOB reports wallet balance=0 for this token.\n"
                        "          Possible causes:\n"
                        "          (a) entry 买单根本没 fill（task 本地 filled_shares 是误报）；\n"
                        "          (b) token_id 错位：entry 买的是 token A，sell 想卖 token B；\n"
                        "          (c) entry 那笔 fill 还没 settle 到 wallet（链上延迟）。"
                    )
                elif balance_shares + 1e-9 < shares_attempted:
                    print(
                        f"      >>> DIAGNOSIS: CLOB balance ({balance_shares:.4f}) < sell attempt ({shares_attempted:.4f}).\n"
                        f"          可能部分 fill 后被吃掉/转出；task 本地状态与 CLOB 出现漂移。"
                    )

        history = task.get_order_history(wallet.wallet_id) if hasattr(task, "get_order_history") else []
        if history:
            print(f"      本地 entry/sell 订单历史 (最近 5 笔):")
            for snap in history[-5:]:
                token_id_short = (snap.token_id[:14] + "...") if snap.token_id and len(snap.token_id) > 14 else snap.token_id
                op = snap.operation.value if hasattr(snap.operation, "value") else str(snap.operation)
                print(
                    f"         - op={op} | side={snap.side.value if snap.side else '?'} | "
                    f"price={snap.price} | shares={snap.shares} | "
                    f"filled_shares={snap.filled_shares} | status={snap.status} | "
                    f"token_id={token_id_short} | order_id={snap.order_id}"
                )
            if not any(s.filled_shares and s.filled_shares > 0 for s in history):
                print(
                    "      >>> DIAGNOSIS: 本地所有订单 filled_shares 都是 0/None ——\n"
                    "          task 认为成交了，但实际没有任何一笔单 fill 报告。"
                )
        else:
            print(
                "      >>> DIAGNOSIS: 本地订单历史为空 —— 这个钱包根本没挂过任何单？"
            )

    def _process_handling_single(self, task: EventTask) -> None:
        """
        处理 HANDLING_SINGLE 状态 - 单边成交处理（挂抛售单，不撤单）。

        业务逻辑：
        - 已成交侧：挂 GTC 抛售单
        - 未成交侧：保留挂单，不撤单
        """
        up_wallet = task.get_up_wallet()
        down_wallet = task.get_down_wallet()

        if not task.first_fill_wallet_id or not up_wallet or not down_wallet:
            task.transition_to(EventTaskState.WAITING_CLOSE_WINDOW, "单边处理完成")
            return

        # 确定已成交侧和未成交侧
        live_wallet = up_wallet if task.first_fill_wallet_id == up_wallet.wallet_id else down_wallet
        stale_wallet = down_wallet if task.first_fill_wallet_id == up_wallet.wallet_id else up_wallet
        live_side = task.side_by_wallet_id.get(live_wallet.wallet_id)
        stale_side = task.side_by_wallet_id.get(stale_wallet.wallet_id)

        # 获取 live 侧的成交信息
        live_order = task.get_order(live_wallet.wallet_id)
        live_filled_shares = coalesce_filled_shares(
            live_order.filled_shares if live_order else None,
            live_order.shares if live_order else None,
        )
        live_filled_amount = float(live_order.filled_amount_usd or 0.0) if live_order else 0.0

        fee_rate_bps = task.metadata.get("fee_rate_bps", 0)

        # 挂 GTC 抛售单，不撤 stale 侧挂单
        sell_shares = live_filled_shares if live_filled_shares > 0 else (live_filled_amount / self.config.fixed_sell_price)

        # 【重试机制说明】
        # 问题：entry 端在 Polymarket V2 CLOB 拿到 ``status="matched"`` 就被标 FILLED，
        #       但 token 还在 on-chain settle 中；挂 sell 时被 CLOB 打回 ``balance: 0``。
        # 修复：余额不足时等待重试，给链上 settle 时间。
        #       重试 3 次，每次间隔 2 秒，共等待 6 秒。
        #       如果最终余额仍不足，才跳过 sell，由强平窗口兜底。
        live_token_id = None
        if task.clob_token_ids and live_side is not None and 0 <= live_side.token_index < len(task.clob_token_ids):
            live_token_id = task.clob_token_ids[live_side.token_index]

        live_balance_shares: float | None = None
        sell_skipped_for_balance = False
        balance_check_passed = False
        balance_retry_count = 0
        max_balance_retries = 3
        balance_retry_interval_sec = 2
        actual_sell_shares = sell_shares  # 最终实际挂单份额

        if live_token_id and not getattr(self.config, "dry_run", False):
            # 首次检查余额
            balance_resp = fetch_token_balance(
                asset_id=live_token_id,
                account=live_wallet.account,
                mock=False,
            )
            live_balance_shares = balance_resp.get("balance_shares")

            # 计算实际可卖份额（取 filled_shares 和余额的较小值）
            actual_sell_shares = min(sell_shares, live_balance_shares or 0)

            # 余额足够，直接挂单
            if balance_resp.get("success") and live_balance_shares is not None and live_balance_shares + 1e-9 >= sell_shares:
                balance_check_passed = True
                print(f"   [余额检查] [{live_wallet.wallet_name}] {live_side.value if live_side else '?'} 侧余额充足: {live_balance_shares:.4f} 股 >= {sell_shares:.4f} 股")
            else:
                # 余额不足，开始重试
                print(f"   [余额不足·重试] [{live_wallet.wallet_name}] {live_side.value if live_side else '?'} 侧 CLOB token 余额 {live_balance_shares or 0:.4f} 股 < 期望 {sell_shares:.4f} 股，开始等待链上 settle...")

                for retry in range(max_balance_retries):
                    balance_retry_count = retry + 1
                    import time
                    print(f"   [余额重试] 等待 {balance_retry_interval_sec}s... ({balance_retry_count}/{max_balance_retries})")
                    time.sleep(balance_retry_interval_sec)

                    balance_resp = fetch_token_balance(
                        asset_id=live_token_id,
                        account=live_wallet.account,
                        mock=False,
                    )
                    live_balance_shares = balance_resp.get("balance_shares")
                    actual_sell_shares = min(sell_shares, live_balance_shares or 0)

                    if balance_resp.get("success") and live_balance_shares is not None and live_balance_shares + 1e-9 >= sell_shares:
                        balance_check_passed = True
                        print(f"   [余额重试成功] [{live_wallet.wallet_name}] {live_side.value if live_side else '?'} 侧余额已充足: {live_balance_shares:.4f} 股 >= {sell_shares:.4f} 股")
                        break
                    else:
                        print(f"   [余额重试] ({balance_retry_count}/{max_balance_retries}) 余额仍不足: {live_balance_shares or 0:.4f} 股 < {sell_shares:.4f} 股")

                if not balance_check_passed:
                    sell_skipped_for_balance = True
                    skip_msg = (
                        f"   [单边] [{live_wallet.wallet_name}] {live_side.value if live_side else '?'} 侧 GTC 抛售单跳过: "
                        f"重试 {max_balance_retries} 次后 CLOB token 余额 {live_balance_shares or 0:.4f} 股 < 期望 {sell_shares:.4f} 股，"
                        f"判定为链上 settle 延迟。强平窗口将直接对该侧执行强平。"
                    )
                    print(skip_msg)
                    TradeLogger.single_filled(live_wallet.wallet_name, live_side.value if live_side else "?", live_filled_shares)
                    TradeLogger.sell_order_failed(
                        live_wallet.wallet_name,
                        live_side.value if live_side else "?",
                        self.config.fixed_sell_price,
                        sell_shares,
                        f"on_chain_balance_mismatch:balance={live_balance_shares or 0:.4f}:retries={balance_retry_count}",
                    )

        # dry_run 模式跳过余额检查，直接挂单
        if getattr(self.config, "dry_run", False):
            balance_check_passed = True

        if sell_skipped_for_balance:
            # 把这次"跳过"留痕为一条本地 FAILED 快照，但不发起任何 CLOB 调用；
            # 下游统计 / 通知与原有抛售失败路径走同一通道。
            skip_snapshot = OrderSnapshot(
                wallet=live_wallet,
                event_name=task.event_name,
                side=live_side,
                amount_usd=float(sell_shares * self.config.fixed_sell_price),
                operation=OperationType.SELL,
                order_id=None,
                token_id=live_token_id,
                condition_id=task.condition_id,
                price=self.config.fixed_sell_price,
                shares=float(sell_shares),
                status=OrderStatus.FAILED.value,
                close_price=self.config.fixed_sell_price,
                filled_amount_usd=0.0,
                filled_shares=0.0,
                error=f"on_chain_balance_mismatch:balance={live_balance_shares or 0:.4f}:retries={balance_retry_count}",
            )
            # 仅追加到 history，不覆盖 current_order
            history_list = task.order_snapshots.setdefault(live_wallet.wallet_id, [])
            history_list.append(skip_snapshot)
        else:
            # 再次检查：Polymarket CLOB 要求最小订单尺寸为 5 股
            MIN_ORDER_SIZE = 5.0
            if actual_sell_shares < MIN_ORDER_SIZE:
                print(
                    f"   [跳过] [{live_wallet.wallet_name}] {live_side.value if live_side else '?'} 侧 GTC 抛售单跳过: "
                    f"实际可卖份额 {actual_sell_shares:.4f} 股 < CLOB 最小要求 {MIN_ORDER_SIZE} 股，"
                    f"强平窗口将直接对该侧执行强平。"
                )
                skip_snapshot = OrderSnapshot(
                    wallet_id=live_wallet.wallet_id,
                    order_id=f"skip_{int(time.time() * 1000)}",
                    operation=OperationType.SELL,
                    side=live_side,
                    price=self.config.fixed_sell_price,
                    shares=sell_shares,
                    status=OrderStatus.SKIPPED.value,
                    timestamp=datetime.now(timezone.utc),
                    close_window=self.config.close_window_sec,
                    amount_usd=0.0,
                    filled_amount_usd=0.0,
                    filled_shares=0.0,
                    error=f"below_min_order_size:actual={actual_sell_shares:.4f}:min={MIN_ORDER_SIZE}",
                )
                history_list = task.order_snapshots.setdefault(live_wallet.wallet_id, [])
                history_list.append(skip_snapshot)
            else:
                sell_result = self._order_exec.place_gtc_sell_order(
                    wallet=live_wallet,
                    event_name=task.event_name,
                    side=live_side,
                    shares=actual_sell_shares,
                    price=self.config.fixed_sell_price,
                    clob_token_ids=task.clob_token_ids,
                    fee_rate_bps=fee_rate_bps,
                    condition_id=task.condition_id,
                )

                # 记录抛售单快照
                if sell_result.snapshot:
                    sell_result.snapshot.status = OrderStatus.SUBMITTED.value if sell_result.outcome.success else OrderStatus.FAILED.value
                    task.mark_order(sell_result.snapshot)

                # 检查挂单是否成功
                sell_success = sell_result.outcome.success
                sell_order_id = sell_result.outcome.order_id if sell_success else None
                sell_err = sell_result.outcome.error or "未知错误"

                # 输出详细日志：包含钱包、方向、份额、价格、订单ID、是否成功
                side_str = live_side.value if live_side else "?"
                if sell_success:
                    print(
                        f"   [单边] [{live_wallet.wallet_name}] {side_str} 侧 GTC 抛售单已挂: "
                        f"{actual_sell_shares:.2f} 股 @ ${self.config.fixed_sell_price:.2f} "
                        f"(balance={live_balance_shares:.4f}, filled={live_filled_shares:.4f}, order_id={sell_order_id})"
                    )
                    TradeLogger.single_filled(live_wallet.wallet_name, side_str, live_filled_shares)
                    TradeLogger.sell_order_placed(live_wallet.wallet_name, side_str, self.config.fixed_sell_price, actual_sell_shares)
                else:
                    # 挂单失败：记录失败原因
                    print(
                        f"   [单边] [{live_wallet.wallet_name}] {side_str} 侧 GTC 抛售单挂单失败: "
                        f"{actual_sell_shares:.2f} 股 @ ${self.config.fixed_sell_price:.2f}, "
                        f"原因: {sell_err}。强平窗口将直接对该侧执行强平。"
                    )
                    TradeLogger.single_filled(live_wallet.wallet_name, side_str, live_filled_shares)
                    TradeLogger.sell_order_failed(live_wallet.wallet_name, side_str, self.config.fixed_sell_price, actual_sell_shares, sell_err)

                    # 诊断：如果失败原因包含 balance，打印诊断信息
                    if "balance" in sell_err.lower():
                        try:
                            self._diagnose_sell_balance_mismatch(
                                task=task,
                                wallet=live_wallet,
                                side=live_side,
                                shares_attempted=actual_sell_shares,
                                price_attempted=self.config.fixed_sell_price,
                                error_text=sell_err,
                            )
                        except Exception:
                            pass

        # 打印 stale 侧状态（用于调试）
        stale_order = task.get_order(stale_wallet.wallet_id)
        stale_status = OrderStatus(stale_order.status).name if stale_order and stale_order.status else "无挂单"
        print(
            f"   [单边] [{stale_wallet.wallet_name}] {stale_side.value if stale_side else '?'} 侧 保留挂单(状态={stale_status}), "
            f"等待强平窗口时再做处理"
        )

        task.trigger_reason = "single_side_fill_pending_close"
        task.transition_to(EventTaskState.WAITING_CLOSE_WINDOW, "单边处理完成，等待强平窗口")

    def _process_waiting_close_window(self, task: EventTask) -> None:
        """
        处理 WAITING_CLOSE_WINDOW 状态 - 等待强平窗口。

        业务逻辑（每秒监控）：
        1. 刷新订单状态
        2. 检查 stale 侧原始挂单是否成交 → 取消抛售单 → SETTLING_OUTCOME
        3. 检查 GTC 抛售单是否成交 → 取消 stale 侧挂单 → SETTLING_OUTCOME
        4. 检查强平窗口到达 → FORCE_CLOSING
        5. 检查事件已结束 → SETTLING_OUTCOME
        """
        # 刷新订单状态
        self._refresh_order_statuses(task)

        # 获取所有钱包的 USDC 余额（用于飞书通知）
        wallet_balances: dict[str, float] = {}
        for wallet in task.wallets:
            try:
                payload = get_wallet_usdc_balance(account=wallet.account)
                if isinstance(payload, dict) and payload.get("success"):
                    balance = payload.get("balance_usdc", 0)
                    wallet_balances[wallet.wallet_name] = float(balance) if balance else 0.0
                else:
                    wallet_balances[wallet.wallet_name] = 0.0
            except Exception:
                wallet_balances[wallet.wallet_name] = 0.0

        end_time_utc = task.end_time
        if end_time_utc.tzinfo is None:
            end_time_utc = end_time_utc.astimezone(timezone.utc)

        now = datetime.now(timezone.utc)
        remaining_to_end = (end_time_utc - now).total_seconds()
        close_window_sec = task.close_window_sec

        up_wallet = task.get_up_wallet()
        down_wallet = task.get_down_wallet()

        if not up_wallet or not down_wallet:
            task.transition_to(EventTaskState.SETTLING_OUTCOME, "钱包信息缺失")
            return

        # 获取 stale 侧（未成交侧）和 live 侧（已成交侧）
        first_fill_id = task.first_fill_wallet_id
        stale_wallet = up_wallet if first_fill_id == down_wallet.wallet_id else down_wallet
        live_wallet = down_wallet if first_fill_id == down_wallet.wallet_id else up_wallet
        stale_side = task.side_by_wallet_id.get(stale_wallet.wallet_id)
        live_side = task.side_by_wallet_id.get(live_wallet.wallet_id)

        stale_order = task.get_order(stale_wallet.wallet_id)
        live_order = task.get_order(live_wallet.wallet_id)

        # 同时检查两边状态
        stale_filled = stale_order and stale_order.status == OrderStatus.FILLED.value
        live_filled = live_order and live_order.status == OrderStatus.FILLED.value

        # 【核心修复】双边都成交时：不再判断"谁先成交"
        # 正确逻辑：检测每个钱包的 entry 仓位和 sell 仓位，分别处理
        if stale_filled and live_filled:
            print("\n   [双边处理] 两边都已成交，检测需要强平的仓位...")

            # 获取所有钱包的 entry 成交份额
            up_filled_shares = coalesce_filled_shares(
                (task.get_order(up_wallet.wallet_id).filled_shares if task.get_order(up_wallet.wallet_id) else None),
                (task.get_order(up_wallet.wallet_id).shares if task.get_order(up_wallet.wallet_id) else None),
            )
            down_filled_shares = coalesce_filled_shares(
                (task.get_order(down_wallet.wallet_id).filled_shares if task.get_order(down_wallet.wallet_id) else None),
                (task.get_order(down_wallet.wallet_id).shares if task.get_order(down_wallet.wallet_id) else None),
            )
            task.up_filled_shares = up_filled_shares
            task.down_filled_shares = down_filled_shares

            # 检查每个钱包的 sell 成交情况
            up_sell_filled = any(
                snap.operation == OperationType.SELL and snap.status == OrderStatus.FILLED.value
                for snap in task.get_order_history(up_wallet.wallet_id)
            )
            down_sell_filled = any(
                snap.operation == OperationType.SELL and snap.status == OrderStatus.FILLED.value
                for snap in task.get_order_history(down_wallet.wallet_id)
            )

            # 执行纯市价单强平（使用配置的 market_close_price）
            close_price = getattr(self.config, "market_close_price", 0.99)
            fee_rate_bps = task.metadata.get("fee_rate_bps", 0)
            wallets_needing_close = []

            if up_filled_shares > 0 and not up_sell_filled:
                wallets_needing_close.append((up_wallet, OrderSide.UP, up_filled_shares))
            if down_filled_shares > 0 and not down_sell_filled:
                wallets_needing_close.append((down_wallet, OrderSide.DOWN, down_filled_shares))

            if wallets_needing_close:
                print(f"   [强平] 检测到 {len(wallets_needing_close)} 个仓位需要强平")

                # 获取所有钱包的 USDC 余额
                wallet_balances: dict[str, float] = {}
                for wallet in task.wallets:
                    try:
                        payload = get_wallet_usdc_balance(account=wallet.account)
                        if isinstance(payload, dict) and payload.get("success"):
                            balance = payload.get("balance_usdc", 0)
                            wallet_balances[wallet.wallet_name] = float(balance) if balance else 0.0
                        else:
                            wallet_balances[wallet.wallet_name] = 0.0
                    except Exception:
                        wallet_balances[wallet.wallet_name] = 0.0

                for wallet, side, shares in wallets_needing_close:
                    print(f"      {wallet.wallet_name} {side.value}: {shares:.4f} @ MARKET (市价)")
                    result = self._order_exec.execute_force_close(
                        wallet=wallet,
                        event_name=task.event_name,
                        side=side,
                        shares=shares,
                        clob_token_ids=task.clob_token_ids,
                        fee_rate_bps=fee_rate_bps,
                        condition_id=task.condition_id,
                    )
                    if result.snapshot:
                        result.snapshot.status = OrderStatus.FILLED.value if result.outcome.success else OrderStatus.FAILED.value
                        task.mark_order(result.snapshot)
                    close_amount = shares * close_price
                    outcome_data = result.outcome
                    filled_shares = outcome_data.get("shares_sold") or outcome_data.get("shares_bought")
                    fill_price = outcome_data.get("fill_price")
                    filled_amount_usd = float(filled_shares) * float(fill_price) if (filled_shares is not None and fill_price is not None) else None
                    result_str = "成功" if outcome_data.success else "失败"
                    TradeLogger.force_close(task.event_name, wallet.wallet_name, side.value, f"${close_amount:.2f}", result_str,
                        filled_shares=filled_shares, fill_price=fill_price, filled_amount_usd=filled_amount_usd)
                    send_force_close_notification(
                        event_name=task.event_name,
                        wallet_name=wallet.wallet_name,
                        side=side.value,
                        shares=shares,
                        price=None,
                        price_note="市价单",
                        result=result_str,
                        is_paper=self._is_paper,
                        wallet_balances=wallet_balances,
                    )
            else:
                print("   ℹ️ 所有仓位均已对冲，无需强平")

            task.trigger_reason = "both_sides_filled_force_closed"
            task.transition_to(EventTaskState.SETTLING_OUTCOME, "双边成交已完成强平")
            return

        if stale_filled and not live_filled:
            # 情况2：stale 侧（entry 挂单）成交，live 侧（sell）未成交
            # 取消 live 侧的 sell 单，让 stale 侧等到强平窗口再处理
            stale_side_val = stale_side.value if stale_side else "?"
            live_side_val = live_side.value if live_side else "?"
            print(f"\n   [双边处理] {stale_wallet.wallet_name}({stale_side_val}) 原始挂单先成交，取消 {live_wallet.wallet_name}({live_side_val}) 的GTC抛售单")

            # 记录 up/down filled_shares
            task.up_filled_shares = coalesce_filled_shares(
                (task.get_order(up_wallet.wallet_id).filled_shares if task.get_order(up_wallet.wallet_id) else None),
                (task.get_order(up_wallet.wallet_id).shares if task.get_order(up_wallet.wallet_id) else None),
            )
            task.down_filled_shares = coalesce_filled_shares(
                (task.get_order(down_wallet.wallet_id).filled_shares if task.get_order(down_wallet.wallet_id) else None),
                (task.get_order(down_wallet.wallet_id).shares if task.get_order(down_wallet.wallet_id) else None),
            )

            # 取消 live 侧的 sell 抛售单
            if live_order and live_order.order_id:
                cancel_result = self._order_exec.cancel_order(
                    live_order.order_id, live_wallet,
                    event_name=task.event_name, side=None
                )
                if cancel_result.snapshot:
                    cancel_result.snapshot.status = OrderStatus.CANCELLED.value
                    task.mark_order(cancel_result.snapshot)
                TradeLogger.order_cancelled(f"{live_wallet.wallet_name}({live_side_val}) GTC抛售单", f"因为{stale_wallet.wallet_name}原始挂单先成交")
                send_cancel_order_notification(
                    event_name=task.event_name,
                    wallet_name=live_wallet.wallet_name,
                    order_type="GTC抛售单",
                    shares=float(live_order.shares or 0) if live_order else 0,
                    reason=f"{stale_wallet.wallet_name} 原始挂单先成交",
                    is_paper=self._is_paper,
                    wallet_balances=wallet_balances,
                )

            task.trigger_reason = "stale_order_filled_first"
            task.transition_to(EventTaskState.SETTLING_OUTCOME, "stale侧挂单先成交")
            return

        elif live_filled and not stale_filled:
            # 情况3：live 侧（sell 抛售单）成交，stale 侧（entry 挂单）未成交
            # live 侧已对冲无需处理，stale 侧的 PLACE 挂单需要取消
            live_side_val = live_side.value if live_side else "?"
            stale_side_val = stale_side.value if stale_side else "?"
            print(f"\n   [双边处理] {live_wallet.wallet_name}({live_side_val}) 抛售单已成交，取消 {stale_wallet.wallet_name}({stale_side_val}) 的原始挂单")

            # 记录 up/down filled_shares
            task.up_filled_shares = coalesce_filled_shares(
                (task.get_order(up_wallet.wallet_id).filled_shares if task.get_order(up_wallet.wallet_id) else None),
                (task.get_order(up_wallet.wallet_id).shares if task.get_order(up_wallet.wallet_id) else None),
            )
            task.down_filled_shares = coalesce_filled_shares(
                (task.get_order(down_wallet.wallet_id).filled_shares if task.get_order(down_wallet.wallet_id) else None),
                (task.get_order(down_wallet.wallet_id).shares if task.get_order(down_wallet.wallet_id) else None),
            )

            # 取消 stale 侧的 PLACE 挂单
            if stale_order and stale_order.order_id:
                stale_side = None if stale_order.operation == OperationType.SELL else stale_order.side
                cancel_result = self._order_exec.cancel_order(
                    stale_order.order_id, stale_wallet,
                    event_name=task.event_name, side=stale_side
                )
                if cancel_result.snapshot:
                    cancel_result.snapshot.status = OrderStatus.CANCELLED.value
                    task.mark_order(cancel_result.snapshot)
                TradeLogger.order_cancelled(f"{stale_wallet.wallet_name}({stale_side_val}) 原始挂单", f"因为{live_wallet.wallet_name}抛售单先成交")
                send_cancel_order_notification(
                    event_name=task.event_name,
                    wallet_name=stale_wallet.wallet_name,
                    order_type="PLACE挂单（entry）",
                    shares=float(stale_order.shares or 0) if stale_order else 0,
                    reason=f"{live_wallet.wallet_name} 抛售单先成交",
                    is_paper=self._is_paper,
                    wallet_balances=wallet_balances,
                )

            # live 侧 sell 已成交，无需强平
            print(f"   ℹ️ {live_wallet.wallet_name} 抛售单已成交，已对冲")

            task.trigger_reason = "sell_order_filled_cancelled_stale"
            task.transition_to(EventTaskState.SETTLING_OUTCOME, "抛售单成交已对冲")
            return

        # 检查 3: 强平窗口到达
        if remaining_to_end <= close_window_sec:
            print(f"   [进入] 强平窗口")
            task.transition_to(EventTaskState.FORCE_CLOSING, "强平窗口到达")
            return

        # 检查 4: 事件已结束
        if remaining_to_end <= 0:
            task.trigger_reason = "event_ended"
            print(f"   [事件结束] 进入结算")
            task.transition_to(EventTaskState.SETTLING_OUTCOME, "事件已结束")
            return

        # 每隔一段时间打印等待状态
        if hasattr(task, '_last_wait_log'):
            log_interval = 30  # 每 30 秒打印一次
            if now.timestamp() - task._last_wait_log > log_interval:
                print(f"   [等待强平] 剩余 {remaining_to_end:.0f}s")
                task._last_wait_log = now.timestamp()
        else:
            task._last_wait_log = now.timestamp()

    def _process_force_closing(self, task: EventTask) -> None:
        """处理 FORCE_CLOSING 状态 - 执行强平。"""
        print("   [执行] 强平...")
        self._execute_force_close(task)
        task.transition_to(EventTaskState.SETTLING_OUTCOME, "强平完成")

    def _process_settling_outcome(self, task: EventTask) -> None:
        """
        处理 SETTLING_OUTCOME 状态 - 轮询市场结果。

        支持两种模式：
        1. 异步模式：使用 AsyncOutcomePoller，后台线程轮询，不阻塞主循环
        2. 同步模式：当异步模式检测到结果时，立即处理
        """
        # 标记开始轮询（持久化）
        self._state_manager.mark_outcome_polling_started(task.event_id)

        # 检查异步轮询器是否已有结果
        if self._async_polling_enabled:
            trade_record = self._state_manager.get_trade(task.event_id)
            if trade_record and trade_record.outcome and trade_record.outcome not in ("UNKNOWN", ""):
                # 异步轮询已有结果，直接处理
                task.outcome = EventOutcome.UP if trade_record.outcome == "UP" else EventOutcome.DOWN
                print(f"   [异步结果] {task.event_name} 市场结果: {task.outcome.value}")
                task.transition_to(EventTaskState.SETTLING_BALANCE, f"结果: {task.outcome.value}")
                return

        # 同步轮询模式（后备或禁用异步时）
        poller_id = f"{task.event_id}_outcome"

        if poller_id not in self._pollers:
            self._pollers[poller_id] = create_outcome_poller(
                condition_id=task.condition_id,
                timeout_sec=self.config.outcome_poll_timeout_sec,
                poll_interval_sec=float(self.config.outcome_poll_interval_sec),
                on_progress=self._handle_poller_progress,
                slug=task.metadata.get("slug"),
                clob_token_ids=task.metadata.get("clob_token_ids") or task.clob_token_ids,
            )
            self._pollers[poller_id].start()
            print("   [同步轮询] 等待市场结果...")

        poller = self._pollers[poller_id]
        result = poller.poll()

        if result.is_complete:
            task.outcome = result.value
            del self._pollers[poller_id]
            outcome_str = task.outcome.value if task.outcome else "UNKNOWN"
            print(f"   [结果] {outcome_str}")

            # 更新持久化状态
            self._state_manager.mark_outcome_polling_completed(task.event_id, outcome_str)

            task.transition_to(EventTaskState.SETTLING_BALANCE, f"结果: {task.outcome.value}")

    def _process_settling_balance(self, task: EventTask) -> None:
        """处理 SETTLING_BALANCE 状态 - 轮询余额稳定。"""
        poller_id = f"{task.event_id}_balance"

        if poller_id not in self._pollers:
            def get_balances() -> dict[str, float | None]:
                balances: dict[str, float | None] = {}
                for wallet in task.wallets:
                    try:
                        payload = get_wallet_usdc_balance(account=wallet.account)
                        balance = payload.get("balance_usdc") if isinstance(payload, dict) else None
                        balances[wallet.wallet_id] = float(balance) if balance else None
                    except Exception:
                        balances[wallet.wallet_id] = None
                return balances

            self._pollers[poller_id] = create_balance_poller(
                wallet_balances_fn=get_balances,
                stable_rounds=self.config.settlement_stable_rounds,
                timeout_sec=self.config.settlement_poll_timeout_sec,
                poll_interval_sec=float(self.config.settlement_poll_interval_sec),
                on_progress=self._handle_poller_progress,
            )
            self._pollers[poller_id].start()
            print("   [等待] 余额稳定...")

        poller = self._pollers[poller_id]
        result = poller.poll()

        if result.is_complete:
            task.final_balances = result.value
            del self._pollers[poller_id]
            print("   [完成] 余额已稳定")
            task.transition_to(EventTaskState.SETTLED, "余额稳定")

    # ===== 核心业务逻辑 =====

    def _cancel_both_entry_orders(self, task: EventTask) -> None:
        """
        取消双边的 entry 挂单（用于 entry_timeout 场景）。

        逻辑：
        1. 获取双边钱包的当前挂单
        2. 只取消 PLACE 类型的挂单（entry 挂单）
        3. 跳过已成交或已是终态的订单
        """
        for wallet in task.wallets:
            order = task.get_order(wallet.wallet_id)
            if not order or not order.order_id:
                continue

            # 只取消 PLACE 类型的 entry 挂单
            if order.operation != OperationType.PLACE:
                continue

            # 跳过已成交或终态订单
            if order.status in {OrderStatus.FILLED.value, OrderStatus.CANCELLED.value}:
                continue

            cancel_result = self._order_exec.cancel_order(
                order_id=order.order_id,
                wallet=wallet,
                event_name=task.event_name,
                side=order.side,
                amount_usd=order.amount_usd,
            )

            cancel_snapshot = cancel_result.snapshot
            if cancel_snapshot:
                cancel_snapshot.status = OrderStatus.CANCELLED.value if cancel_result.outcome.success else OrderStatus.FAILED.value
                task.mark_order(cancel_snapshot)

            status_str = "成功" if cancel_result.outcome.success else "失败"
            print(f"      [{wallet.wallet_name}] 取消 entry 挂单: {status_str}")
            TradeLogger.order_cancelled(wallet.wallet_name, "entry_timeout 取消")

            # 获取钱包余额用于通知
            wallet_balances_cancel: dict[str, float] = {}
            for w in task.wallets:
                try:
                    payload = get_wallet_usdc_balance(account=w.account)
                    if isinstance(payload, dict) and payload.get("success"):
                        balance = payload.get("balance_usdc", 0)
                        wallet_balances_cancel[w.wallet_name] = float(balance) if balance else 0.0
                    else:
                        wallet_balances_cancel[w.wallet_name] = 0.0
                except Exception:
                    wallet_balances_cancel[w.wallet_name] = 0.0

            # 发送飞书通知：挂单超时取消
            send_cancel_order_notification(
                event_name=task.event_name,
                wallet_name=wallet.wallet_name,
                order_type="PLACE挂单（entry）",
                shares=float(order.shares or 0),
                reason="等待成交超时（entry_timeout）",
                is_paper=self._is_paper,
                wallet_balances=wallet_balances_cancel,
            )

    # 同一 task 在 MIN_REFRESH_INTERVAL_SEC 内的重复调用直接早返回。
    # 避免 100ms tick + 多 wallet 并发时，订单状态查询把 Polymarket CLOB 的
    # 公开 GET /orders/{id} 限速打爆（12 task × 2 wallet × 10Hz = 240 req/s）。
    # fill 检测延迟上限 ≈ MIN_REFRESH_INTERVAL_SEC，业务无感（5m 市场限价单 fill < 1s）。
    MIN_REFRESH_INTERVAL_SEC = 0.25

    def _refresh_order_statuses(self, task: EventTask) -> None:
        """
        刷新订单状态。

        采用 ThreadPoolExecutor 并发调用 fetch_order_status，把多个钱包的 HTTP 查询
        从串行改成并行，整体延迟从 O(N * T) 降到 O(T)（N 为 wallet 数，T 为单次 HTTP 延迟）。

        状态归一化、mark_order、日志等下游处理仍按 wallet 顺序执行，
        与原串行实现的逻辑判定完全等价。

        节流：tick=100ms 时，同 task 的实际 fetch 频率上限为 1/MIN_REFRESH_INTERVAL_SEC = 4Hz。
        多 task 并发场景下 HTTP 流量 = task 数 × wallet 数 × 4 / IP，远低于 CLOB 公开限速。
        """
        # ===== 第 0 步：节流早返回 =====
        # _last_refresh_time 是 task 实例属性，首次访问默认 0.0 → 首次一定执行
        now_mono = time.monotonic()
        last_refresh = getattr(task, "_last_refresh_time", 0.0)
        if now_mono - last_refresh < self.MIN_REFRESH_INTERVAL_SEC:
            return
        # 不在这里更新 _last_refresh_time，等 API 成功后再更新
        now_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]

        # ===== 第 1 步：按 wallet 收集"待刷新"的订单快照（保留原过滤逻辑）=====
        pending: list[tuple[WalletIdentity, OrderSnapshot]] = []
        for wallet in task.wallets:
            snapshot = task.get_order(wallet.wallet_id)
            if not snapshot or not snapshot.order_id:
                continue
            if snapshot.operation not in {OperationType.PLACE, OperationType.SELL}:
                continue
            # 只对 SUBMITTED 状态的订单调用 API 检查
            if snapshot.status != OrderStatus.SUBMITTED.value:
                continue
            pending.append((wallet, snapshot))

        if not pending:
            # 没有待刷新订单，但仍更新 throttle 时间戳
            task._last_refresh_time = time.monotonic()
            return

        # ===== 第 2 步：并发执行 fetch_order_status =====
        # 每个 wallet 一个 worker；数量 ≤ wallet 数
        results: dict[str, tuple[ExecutionOutcome, OrderSnapshot | None, WalletIdentity]] = {}

        def _fetch_one(wallet: WalletIdentity, snapshot: OrderSnapshot) -> tuple[str, tuple[ExecutionOutcome, OrderSnapshot | None, WalletIdentity]]:
            try:
                outcome, _ = self._order_exec.refresh_order_status(
                    order_id=snapshot.order_id,
                    wallet=wallet,
                )
            except Exception:
                raise  # 异常在主线程捕获并打印 traceback
            return wallet.wallet_id, (outcome, None, wallet)

        max_workers = max(1, min(len(pending), 8))
        # 软超时：单个 HTTP 请求超过此时间则视为失败，释放 tick 继续推进
        FETCH_TIMEOUT_SEC = 2.0

        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="order-refresh") as pool:
            futures = {
                pool.submit(_fetch_one, wallet, snapshot): wallet.wallet_id
                for wallet, snapshot in pending
            }
            for fut in as_completed(futures):
                wallet_id = futures[fut]
                try:
                    wid, payload = fut.result(timeout=FETCH_TIMEOUT_SEC)
                except TimeoutError:
                    # 单 wallet 超时不阻塞 tick：视为该 wallet 本轮刷新失败
                    print(f"      ⚠️ 订单状态刷新超时 wallet={wallet_id}，本轮跳过")
                    continue
                except Exception as exc:
                    # 异常直接抛出，打印完整 traceback
                    import traceback
                    print(f"      ⚠️ 订单状态刷新异常 wallet={wallet_id}: {exc}")
                    traceback.print_exc()
                    raise  # 直接抛出，不吞掉
                results[wid] = payload

        # ===== 第 3 步：按 wallet 原顺序处理结果，保持与旧串行实现相同的判定 =====
        for wallet, snapshot in pending:
            payload = results.get(wallet.wallet_id)
            if payload is None:
                continue
            outcome, _, _ = payload

            prev_status = snapshot.status

            # 原逻辑：失败直接 continue，不更新 snapshot
            if not outcome.success:
                print(f"      ⚠️ 订单状态刷新失败 {wallet.wallet_name}: {outcome.error or 'unknown_error'}")
                continue

            raw = outcome.raw if isinstance(outcome.raw, dict) else {}
            status = str(raw.get("status") or "").lower()

            # 归一化状态
            normalized_status = normalize_clob_status(status)

            # 原逻辑：未识别状态直接 continue
            if normalized_status not in {
                OrderStatus.SUBMITTED.value,
                OrderStatus.FILLED.value,
                OrderStatus.CANCELLED.value,
                OrderStatus.FAILED.value,
            }:
                print(f"      ⚠️ 订单状态无法识别 {wallet.wallet_name}: raw_status={status}, normalized={normalized_status}")
                continue

            snapshot.status = normalized_status
            snapshot.raw_status = outcome.raw_status or raw.get("raw_status") or raw.get("status")
            if outcome.shares is not None:
                snapshot.shares = float(outcome.shares)
            if outcome.filled_shares is not None:
                snapshot.filled_shares = float(outcome.filled_shares)
            if outcome.filled_amount_usd is not None:
                snapshot.filled_amount_usd = float(outcome.filled_amount_usd)
            if outcome.average_fill_price is not None:
                snapshot.average_fill_price = float(outcome.average_fill_price)

            task.mark_order(snapshot)

            # 状态变化时打印诊断日志（与原格式完全一致）
            new_status = snapshot.status
            if prev_status != new_status:
                op_type = snapshot.operation.value if hasattr(snapshot.operation, 'value') else str(snapshot.operation)
                print(f"[{now_str}] [订单状态变化] {wallet.wallet_name} | 操作={op_type} | {prev_status} → {new_status} | order_id={snapshot.order_id[:16]}...")

        # API 调用全部成功后才更新 throttle 时间戳，失败时不重置
        # 这样 API 不稳定时重试仍遵守 250ms 节流，避免连击触发限流
        task._last_refresh_time = time.monotonic()

    def _execute_force_close(self, task: EventTask) -> None:
        """执行强平。"""
        close_window_sec = task.close_window_sec
        selected_accounts = [f"{wallet.wallet_name}({wallet.account.account_id})" for wallet in task.wallets]
        is_single_side_pending = task.trigger_reason == "single_side_fill_pending_close"

        # 获取每个钱包的初始 entry 成交份额
        entry_filled_by_wallet: dict[str, float] = {}
        if is_single_side_pending and task.first_fill_wallet_id:
            for wallet in task.wallets:
                for snap in task.get_order_history(wallet.wallet_id):
                    if snap.operation == OperationType.PLACE and snap.status == OrderStatus.FILLED.value:
                        entry_filled_by_wallet[wallet.wallet_id] = coalesce_filled_shares(snap.filled_shares, snap.shares)
                        break

        # 检查 GTC 抛售单是否已被市场吃掉
        sell_order_filled_by_wallet: dict[str, bool] = {}
        for wallet in task.wallets:
            sell_order_filled_by_wallet[wallet.wallet_id] = any(
                snap.operation == OperationType.SELL and snap.status == OrderStatus.FILLED.value
                for snap in task.get_order_history(wallet.wallet_id)
            )

        # 检查是否有任何钱包的抛售单已成交
        # 如果有，则只撤单不执行强平（因为抛售单已成交相当于已对冲）
        any_sell_filled = any(sell_order_filled_by_wallet.get(w.wallet_id, False) for w in task.wallets)

        print(f"\n⚡ 开始执行强平: {task.event_name} | 窗口={close_window_sec}s | 触发={task.trigger_reason}")
        if any_sell_filled:
            print(f"   ℹ️ 检测到抛售单已成交，只撤单不执行强平")

        # 发送飞书通知：强平窗口触发
        wallet_statuses = []

        # 获取所有钱包的 USDC 余额
        wallet_balances: dict[str, float] = {}
        for w in task.wallets:
            try:
                payload = get_wallet_usdc_balance(account=w.account)
                if isinstance(payload, dict) and payload.get("success"):
                    balance = payload.get("balance_usdc", 0)
                    wallet_balances[w.wallet_name] = float(balance) if balance else 0.0
                else:
                    wallet_balances[w.wallet_name] = 0.0
            except Exception:
                wallet_balances[w.wallet_name] = 0.0

        for w in task.wallets:
            entry_shares = entry_filled_by_wallet.get(w.wallet_id, 0.0)
            sell_filled = sell_order_filled_by_wallet.get(w.wallet_id, False)
            side = task.side_by_wallet_id.get(w.wallet_id, None)
            wallet_statuses.append({
                "wallet_name": w.wallet_name,
                "filled_shares": entry_shares,
                "side": side.value if side else "N/A",
                "hedged": sell_filled,
            })
        send_close_window_notification(
            event_name=task.event_name,
            wallets=wallet_statuses,
            trigger_reason=task.trigger_reason or "强平窗口到期",
            is_paper=self._is_paper,
            wallet_balances=wallet_balances,
        )

        fee_rate_bps = task.metadata.get("fee_rate_bps", 0)

        for wallet in task.wallets:
            order = task.get_order(wallet.wallet_id)
            wallet_name = wallet.wallet_name

            # 获取 entry 成交份额（从历史订单中查找）
            entry_filled_shares = 0.0
            if is_single_side_pending:
                # 单边场景：优先从预计算的 entry_filled_by_wallet 获取（entry 买单的成交份额）
                if wallet.wallet_id in entry_filled_by_wallet:
                    entry_filled_shares = entry_filled_by_wallet[wallet.wallet_id]
                else:
                    # 兜底：从订单历史中查找 PLACE 类型且已成交的记录
                    for snap in task.get_order_history(wallet.wallet_id):
                        if snap.operation == OperationType.PLACE and snap.status == OrderStatus.FILLED.value:
                            entry_filled_shares = coalesce_filled_shares(snap.filled_shares, snap.shares)
                            break
            elif order:
                entry_filled_shares = coalesce_filled_shares(order.filled_shares, order.shares)

            # 判断当前挂单类型
            is_sell_order = order and order.operation == OperationType.SELL
            is_entry_order = order and order.operation == OperationType.PLACE

            # 获取当前钱包的 entry 方向对应的 token_id（用于余额检查）
            wallet_entry_side = task.side_by_wallet_id.get(wallet.wallet_id)
            live_token_id = None
            if task.clob_token_ids and wallet_entry_side is not None and 0 <= wallet_entry_side.token_index < len(task.clob_token_ids):
                live_token_id = task.clob_token_ids[wallet_entry_side.token_index]

            # 检查是否有未成交的挂单（部分成交后还有剩余份额）
            order_filled = float(order.filled_shares or 0) if order else 0
            order_total = float(order.shares or 0) if order else 0
            has_unfilled = order_total > order_filled

            # 如果 GTC 抛售单已被市场吃掉，跳过
            if is_single_side_pending and sell_order_filled_by_wallet.get(wallet.wallet_id, False):
                print(f"   ⏭️ {wallet_name}: 抛售单已成交，跳过")
                continue

            # 尝试撤单（只有存在未成交份额时才撤单）
            cancel_already_terminal = False
            if order and order.order_id and order.status == OrderStatus.SUBMITTED.value and has_unfilled:
                cancel_outcome = self._order_exec.cancel_order(
                    order_id=order.order_id,
                    wallet=wallet,
                    event_name=task.event_name,
                    side=order.side,
                    amount_usd=order.amount_usd,
                )

                # 记录撤单快照
                cancel_snapshot = cancel_outcome.snapshot
                if cancel_snapshot:
                    cancel_snapshot.status = OrderStatus.CANCELLED.value if cancel_outcome.success else OrderStatus.FAILED.value
                    task.mark_order(cancel_snapshot)

                TradeLogger.order_cancelled(wallet_name)

                # 发送飞书通知
                send_cancel_order_notification(
                    event_name=task.event_name,
                    wallet_name=wallet_name,
                    order_type="GTC抛售单" if is_sell_order else "PLACE挂单",
                    shares=float(order.shares or 0),
                    reason="强平窗口触发，清理未成交订单",
                    is_paper=self._is_paper,
                    wallet_balances=wallet_balances,
                )

                # 撤单后检查实际状态
                refresh_outcome, refresh_snapshot = self._order_exec.refresh_order_status(
                    order_id=order.order_id,
                    wallet=wallet,
                )
                if refresh_outcome.success and isinstance(refresh_outcome.raw, dict):
                    latest_status = str(refresh_outcome.raw.get("status") or "").lower()
                    if latest_status in CLOB_FILLED_STATUSES:
                        order.status = OrderStatus.FILLED.value
                        task.mark_order(order)
                        print(f"   ⚠️ {wallet_name}: 撤单时发现已成交")
                        cancel_already_terminal = True
                    elif latest_status in CLOB_CANCELLED_STATUSES:
                        order.status = OrderStatus.CANCELLED.value
                        task.mark_order(order)
                        cancel_already_terminal = True
                    elif not cancel_outcome.success:
                        print(f"   ⚠️ {wallet_name}: 撤单失败，订单仍在簿上")
                        cancel_already_terminal = True

            # 如果是 SELL 抛售单，检查是否需要强平平掉 entry 仓位
            if is_sell_order:
                # 无论撤单成功与否，只要有 entry 仓位且抛售单未成交，都需要强平
                if entry_filled_shares > 0 and not any_sell_filled:
                    # 确定强平方向：与 entry 方向相同（卖出持有的 token 来平仓）
                    wallet_entry_side = task.side_by_wallet_id.get(wallet.wallet_id)
                    close_side = wallet_entry_side  # 平仓方向 = entry 方向

                    # 先取消现有的 GTC 抛售单（确保清理干净）
                    if order and order.order_id and order.status == OrderStatus.SUBMITTED.value:
                        if not cancel_already_terminal:
                            # 撤单未成功，尝试再次撤单
                            self._order_exec.cancel_order(
                                order_id=order.order_id,
                                wallet=wallet,
                                event_name=task.event_name,
                                side=order.side,
                                amount_usd=order.amount_usd,
                            )
                            print(f"   🗑️ {wallet_name}: 取消现有 GTC 抛售单")

                    # 强平前检查余额，取 min(entry_filled_shares, balance)
                    MIN_ORDER_SIZE = 5.0
                    actual_close_shares = entry_filled_shares
                    if live_token_id and not getattr(self.config, "dry_run", False):
                        balance_resp = fetch_token_balance(
                            asset_id=live_token_id,
                            account=wallet.account,
                            mock=False,
                        )
                        live_balance = balance_resp.get("balance_shares") or 0
                        actual_close_shares = min(entry_filled_shares, live_balance)
                        if actual_close_shares < MIN_ORDER_SIZE:
                            print(f"   [跳过] {wallet_name} {close_side.value}侧: 可平份额 {actual_close_shares:.4f} < CLOB最小{MIN_ORDER_SIZE}，跳过")
                            # 记录跳过并继续处理下一个钱包
                            TradeLogger.force_close(task.event_name, wallet_name, close_side.value, "$0.00", "跳过(不足最小尺寸)")
                            continue

                    # 执行纯市价单强平
                    print(f"   🔄 {wallet_name}: 执行纯市价单强平 {actual_close_shares:.2f} @ MARKET (市价)")
                    close_result = self._order_exec.execute_force_close(
                        wallet=wallet,
                        event_name=task.event_name,
                        side=close_side,
                        shares=actual_close_shares,
                        clob_token_ids=task.clob_token_ids,
                        fee_rate_bps=fee_rate_bps,
                        condition_id=task.condition_id,
                    )
                    if close_result.snapshot:
                        close_result.snapshot.status = OrderStatus.FILLED.value if close_result.outcome.success else OrderStatus.FAILED.value
                        task.mark_order(close_result.snapshot)

                    # 从 outcome 中提取实际成交数据
                    outcome_data = close_result.outcome
                    filled_shares = outcome_data.get("shares_sold") or outcome_data.get("shares_bought")
                    fill_price = outcome_data.get("fill_price")
                    filled_amount_usd = None
                    if filled_shares is not None and fill_price is not None:
                        filled_amount_usd = float(filled_shares) * float(fill_price)

                    result_str = "成功" if outcome_data.success else "失败"
                    TradeLogger.force_close(
                        task.event_name, wallet_name, close_side.value,
                        f"${entry_filled_shares * self.config.fixed_sell_price:.2f}",
                        result_str,
                        filled_shares=filled_shares,
                        fill_price=fill_price,
                        filled_amount_usd=filled_amount_usd,
                    )

                    # 发送飞书通知：强平结果
                    send_force_close_notification(
                        event_name=task.event_name,
                        wallet_name=wallet_name,
                        side=close_side.value,
                        shares=entry_filled_shares,
                        price=None,
                        price_note="市价单",
                        result=result_str,
                        reason="抛售单未成交，强平窗口触发",
                        is_paper=self._is_paper,
                        wallet_balances=wallet_balances,
                    )
                else:
                    # entry 无仓位或已对冲，只需清理抛售单
                    if order and order.order_id and order.status == OrderStatus.SUBMITTED.value:
                        if not cancel_already_terminal:
                            self._order_exec.cancel_order(
                                order_id=order.order_id,
                                wallet=wallet,
                                event_name=task.event_name,
                                side=order.side,
                                amount_usd=order.amount_usd,
                            )
                            print(f"   🗑️ {wallet_name}: 取消 GTC 抛售单（entry 无仓位）")
                        else:
                            print(f"   🗑️ {wallet_name}: GTC 抛售单已取消")
                    print(f"   ⏭️ {wallet_name}: 跳过（entry 无仓位或已对冲）")
                continue

            if cancel_already_terminal:
                continue

            # 执行纯市价单强平的条件：
            # 1. 当前挂单是 PLACE（entry）
            # 2. 有成交份额
            # 3. 没有 SELL 抛售单已成交（any_sell_filled）
            should_close = is_entry_order and entry_filled_shares > 0 and not any_sell_filled

            if not should_close:
                reason = "抛售单已成交" if any_sell_filled else ("成交份额=0" if entry_filled_shares <= 0 else "当前挂单是 SELL 抛售单")
                # 即使不执行强平，如果有未成交的 PLACE 挂单，也要先撤单
                if is_entry_order and order and order.order_id and order.status == OrderStatus.SUBMITTED.value:
                    self._order_exec.cancel_order(
                        order_id=order.order_id,
                        wallet=wallet,
                        event_name=task.event_name,
                        side=order.side,
                        amount_usd=order.amount_usd,
                    )
                    print(f"   🗑️ {wallet_name}: 强平窗口-取消 PLACE 挂单 (reason={reason})")
                    send_cancel_order_notification(
                        event_name=task.event_name,
                        wallet_name=wallet_name,
                        order_type="PLACE挂单",
                        shares=float(order.shares or 0),
                        reason=f"强平窗口触发: {reason}",
                        is_paper=self._is_paper,
                        wallet_balances=wallet_balances,
                    )
                print(f"   ⏭️ {wallet_name}: {reason}，跳过纯市价单强平")
                continue

            # 执行纯市价单强平：先取消现有的 PLACE 挂单
            if order and order.order_id and order.status == OrderStatus.SUBMITTED.value:
                self._order_exec.cancel_order(
                    order_id=order.order_id,
                    wallet=wallet,
                    event_name=task.event_name,
                    side=order.side,
                    amount_usd=order.amount_usd,
                )
                print(f"   🗑️ {wallet_name}: 取消现有 PLACE 挂单")

                # 发送飞书通知
                send_cancel_order_notification(
                    event_name=task.event_name,
                    wallet_name=wallet_name,
                    order_type="PLACE挂单",
                    shares=float(order.shares or 0),
                    reason="强平窗口触发，清理未成交订单",
                    is_paper=self._is_paper,
                    wallet_balances=wallet_balances,
                )

            side = order.side if order else OrderSide.UP
            print(f"   🔄 {wallet_name}: 执行纯市价单强平 {entry_filled_shares:.2f} @ MARKET (市价)")
            close_result = self._order_exec.execute_force_close(
                wallet=wallet,
                event_name=task.event_name,
                side=side,
                shares=entry_filled_shares,
                clob_token_ids=task.clob_token_ids,
                fee_rate_bps=fee_rate_bps,
                condition_id=task.condition_id,
            )

            # 记录强平快照
            if close_result.snapshot:
                close_result.snapshot.status = OrderStatus.FILLED.value if close_result.outcome.success else OrderStatus.FAILED.value
                task.mark_order(close_result.snapshot)

            # 打印日志
            close_price = self.config.fixed_sell_price
            close_amount = entry_filled_shares * close_price
            outcome_data = close_result.outcome
            filled_shares = outcome_data.get("shares_sold") or outcome_data.get("shares_bought")
            fill_price = outcome_data.get("fill_price")
            filled_amount_usd = float(filled_shares) * float(fill_price) if (filled_shares is not None and fill_price is not None) else None
            result_str = "成功" if outcome_data.success else "失败"
            TradeLogger.force_close(task.event_name, wallet_name, side.value, f"${close_amount:.2f}", result_str,
                filled_shares=filled_shares, fill_price=fill_price, filled_amount_usd=filled_amount_usd)

            # 发送飞书通知：强平结果
            send_force_close_notification(
                event_name=task.event_name,
                wallet_name=wallet_name,
                side=side.value,
                shares=entry_filled_shares,
                price=None,
                price_note="市价单",
                result=result_str,
                reason="强平窗口触发",
                is_paper=self._is_paper,
                wallet_balances=wallet_balances,
            )

    # ===== 完成处理 =====

    def _finalize_task(self, task: EventTask) -> None:
        """完成任务的最终处理。"""
        # 跳过的任务打印原因
        if task.state == EventTaskState.SKIPPED:
            print(f"   [跳过] {task.event_name} - {task.trigger_reason or '时间不足'}")
            self._completed_skipped_count += 1
            return

        # 计算 PnL
        for wallet in task.wallets:
            start_balance = self._start_balances.get(wallet.wallet_id)
            end_balance = task.final_balances.get(wallet.wallet_id)
            if start_balance is not None and end_balance is not None:
                task.pnl_by_wallet[wallet.wallet_id] = round(end_balance - start_balance, 6)

        task.total_pnl = round(sum(task.pnl_by_wallet.values()), 6)
        task.is_profit = task.total_pnl > 0

        # 更新损失窗口
        self._loss_window.record(task.is_profit)

        # 检查是否需要停机
        consecutive_losses = self._loss_window.consecutive_losses()
        if self._loss_window.should_halt(self.config.max_consecutive_losses):
            self._halted = True
            self._halt_reason = f"连续亏损 {consecutive_losses}/{self.config.max_consecutive_losses} 次"
            TradeLogger.halt(self._halt_reason)

        # 打印结果
        TradeLogger.event_result(task.total_pnl, task.is_profit, task.pnl_by_wallet)

        # 导出结果
        self._export_result(task)

    def _cleanup_terminal_tasks(self) -> None:
        """清理已终止的任务。"""
        for event_id, task in list(self._tasks.items()):
            if task.is_terminal:
                self._tasks.pop(event_id)
                if task.state == EventTaskState.SETTLED:
                    self._completed_tasks.append(task)
                elif task.state == EventTaskState.FAILED:
                    self._failed_tasks.append(task)

    # ===== 回调处理 =====

    def _handle_state_change(
        self,
        task: EventTask,
        old_state: EventTaskState,
        new_state: EventTaskState,
    ) -> None:
        """处理状态变化。"""
        # 状态转移不再打印冗余日志，只在关键状态时提示

    def _handle_task_progress(self, task: EventTask) -> None:
        """处理任务进度日志。"""
        if task.should_log_progress(self.config.progress_log_interval_sec):
            remaining_end = task.remaining_to_end()
            TradeLogger.waiting_close_window(remaining_end)

    def _handle_task_complete(self, task: EventTask) -> None:
        """处理任务完成。"""
        # 结果已在 _finalize_task 中打印

    def _handle_poller_progress(self, poller: Poller) -> None:
        """处理轮询器进度日志。"""
        status = poller.get_status()
        remaining = status.get("remaining_sec", 0)
        rounds = status.get("rounds", 0)
        TradeLogger.poll_progress(poller.name, str(rounds), f"{remaining:.0f}")

    # ===== 日志和导出 =====

    def _log_order_submission(
        self,
        wallet: WalletIdentity,
        side: OrderSide | None,
        amount_usd: float,
        snapshot: Any,
    ) -> None:
        """打印挂单日志。"""
        import core.config as config_module
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        side_text = side.value if side else "UNKNOWN"
        if snapshot and hasattr(snapshot, "price"):
            price = snapshot.price
            shares = amount_usd / price if price > 0 else 0
            status = snapshot.status if hasattr(snapshot, "status") else "unknown"
            token_id = snapshot.clob_token_id if hasattr(snapshot, "clob_token_id") else "N/A"
            order_id = snapshot.order_id if hasattr(snapshot, "order_id") else "N/A"
            error_text = snapshot.error if hasattr(snapshot, "error") and snapshot.error else ""
            if error_text:
                print(f"[{now_str}] 【挂单】{wallet.wallet_name}({wallet.wallet_id}) | side={side_text} | amount_usd={amount_usd:.4f} | price={price:.4f} | shares={shares:.4f} | status={status} | error={error_text}")
            else:
                print(f"[{now_str}] 【挂单】{wallet.wallet_name}({wallet.wallet_id}) | side={side_text} | amount_usd={amount_usd:.4f} | price={price:.4f} | shares={shares:.4f} | status={status} | token_id={token_id} | order_id={order_id}")

    def _export_result(self, task: EventTask) -> None:
        """导出结果。"""
        # 构建兼容旧架构的 state 和 summary
        state = self._build_legacy_state(task)
        summary = self._build_legacy_summary(task)

        # 导出 Excel
        export_dual_wallet_event_to_excel(
            run_folder=self.run_folder,
            event_state=state,
            summary=summary,
            dry_run=self.dry_run,
        )

        # 发送飞书通知（只在有强平操作时发送）
        if self.config.enable_feishu and self._has_close_operation(task):
            try:
                # 格式化飞书消息
                emoji = "⚡" if task.is_profit else "⚠️"
                pnl_sign = "+" if task.is_profit else "-"
                outcome_icon = "📊 YES" if task.outcome == EventOutcome.YES else "📊 NO" if task.outcome == EventOutcome.NO else "📊 ?"

                wallet_lines = []
                for wallet_id, pnl in task.pnl_by_wallet.items():
                    balance = task.final_balances.get(wallet_id)
                    balance_text = "UNKNOWN" if balance is None else f"${balance:.2f}"
                    pnl_emoji = "✅" if pnl >= 0 else "❌"
                    wallet_lines.append(f"{pnl_emoji} {wallet_id}: {pnl_emoji} ${pnl:.4f} | 余额: {balance_text}")

                content = f"""📋 事件: {task.event_name}

{emoji} 结果: {'盈利' if task.is_profit else '亏损'} | 总收益: {pnl_sign}${abs(task.total_pnl):.4f}

{outcome_icon} 市场结果: {task.outcome.value}

💡 触发方式: {task.trigger_reason or '无'}

{self._get_trigger_description(task)}

{'📊 钱包明细:' + chr(10) + chr(10).join(wallet_lines) if wallet_lines else ''}

⚠️ 连续亏损: {self._loss_window.consecutive_losses()} / {self.config.max_consecutive_losses}"""

                send_feishu(
                    title=f"{emoji} 强平 | {task.event_name}",
                    content=content,
                    level="success" if task.is_profit else "warn",
                )
            except Exception:
                pass

    def _has_actual_trades(self, task: EventTask) -> bool:
        """检查是否真正发生了交易（至少有一边成交）。"""
        for wallet_id, history in task.order_snapshots.items():
            for snapshot in history:
                if snapshot.status == OrderStatus.FILLED.value and snapshot.operation in {
                    OperationType.PLACE,
                    OperationType.SELL,
                    OperationType.FORCE_CLOSE
                }:
                    return True
        return False

    def _has_close_operation(self, task: EventTask) -> bool:
        """检查是否有强平操作。"""
        for wallet_id, history in task.order_snapshots.items():
            for snapshot in history:
                if snapshot.operation == OperationType.FORCE_CLOSE:
                    return True
        return False

    def _get_trigger_description(self, task: EventTask) -> str:
        """获取触发方式的描述。"""
        descriptions = {
            "both_sides_filled": "📌 双边全部成交",
            "single_side_filled": "📌 单边成交，触发对冲",
            "entry_timeout": "📌 挂单超时，进入结算",
            "force_close_window": "📌 进入强平窗口",
            "stale_order_filled_first": "📌 未成交侧先成交，取消抛售单",
            "sell_order_filled_first": "📌 抛售单先成交，取消未成交侧",
            "event_ended": "📌 事件已结束",
        }
        return descriptions.get(task.trigger_reason or "", f"📌 {task.trigger_reason or '未知'}")

    def _build_legacy_state(self, task: EventTask) -> DualWalletEventState:
        """构建兼容旧架构的 DualWalletEventState。"""
        return DualWalletEventState(
            event_name=task.event_name,
            event_id=task.event_id,
            start_time=task.start_time or datetime.now(timezone.utc),
            end_time=task.end_time,
            close_price=task.metadata.get("close_price", 0),
            close_window_sec=task.close_window_sec,
            x_timeout_sec=self.config.entry_timeout_sec,
            flow_state=EventFlowState.SETTLED,
            wallet_orders=task.current_orders,
            wallet_order_history=task.order_snapshots,
            side_by_wallet_id=task.side_by_wallet_id,
            first_fill_wallet_id=task.first_fill_wallet_id,
            second_fill_wallet_id=task.second_fill_wallet_id,
            outcome=task.outcome,
            trigger_reason=task.trigger_reason,
            trigger_detail=task.trigger_detail,
        )

    def _build_legacy_summary(self, task: EventTask) -> Any:
        """构建兼容旧架构的 EventResultSummary。"""
        from strategy.dual_wallet_models import EventResultSummary

        filled = 0
        cancelled = 0
        force_closed = 0
        for wallet_id, history in task.order_snapshots.items():
            for snapshot in history:
                if snapshot.status == OrderStatus.FILLED.value:
                    filled += 1
                if snapshot.operation == OperationType.CANCEL and snapshot.status == OrderStatus.CANCELLED.value:
                    cancelled += 1
                if snapshot.operation == OperationType.FORCE_CLOSE and snapshot.status == OrderStatus.FILLED.value:
                    force_closed += 1

        return EventResultSummary(
            event_name=task.event_name,
            outcome=task.outcome,
            total_pnl_usd=task.total_pnl,
            wallet_pnl_usd=task.pnl_by_wallet,
            wallet_balance_usdc=task.final_balances,
            order_count=sum(len(h) for h in task.order_snapshots.values()),
            filled_count=filled,
            cancelled_count=cancelled,
            force_closed_count=force_closed,
            is_profit=task.is_profit,
            settled_at=datetime.now(timezone.utc),
        )

    # ===== 查询方法 =====

    def should_halt(self) -> bool:
        """是否应该停机。"""
        return self._halted

    def halt_reason(self) -> str | None:
        """获取停机原因。"""
        return self._halt_reason

    def get_summary(self) -> dict:
        """获取管理器摘要。"""
        return {
            "active_count": len(self.active_tasks),
            "completed_count": len(self._completed_tasks),
            "failed_count": len(self._failed_tasks),
            "total_count": len(self._tasks),
            "halted": self._halted,
            "halt_reason": self._halt_reason,
            "consecutive_losses": self._loss_window.consecutive_losses(),
            "active_states": {
                get_state_display_name(t.state): [
                    t.event_name for t in self.active_tasks if t.state == t.state
                ]
            },
        }

    def run(self) -> None:
        """
        运行主循环。

        这是 TaskManager 的默认运行方式，适合单线程环境。
        """
        TradeLogger.manager_start(str(self.active_count))
        while self.active_count > 0 and not self.should_halt():
            completed = self.tick()
            if completed:
                TradeLogger.manager_complete(str(len(completed)), str(self.active_count))
            time.sleep(self.config.poll_interval_sec)

        TradeLogger.manager_stop(str(len(self._completed_tasks)), str(len(self._failed_tasks)))
