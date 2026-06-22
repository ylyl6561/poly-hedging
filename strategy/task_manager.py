"""
任务管理器 (TaskManager) - 新架构的核心调度器。

整合了所有业务逻辑，支持：
1. 挂初始买单 (GTC)
2. 撤单 + 挂抛售单 (GTC)
3. 执行强平 (FAK)
4. 轮询市场结果
5. 轮询余额稳定

与原有的 run_event 方法完全兼容，业务逻辑保持一致。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable


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
    def waiting_close_window(cls, remaining: float) -> None:
        """等待强平窗口。"""
        print(f"\n   [强平窗口] 剩余 {remaining:.0f}s...")

    @classmethod
    def close_window_reached(cls) -> None:
        """强平窗口到达。"""
        print(f"   [强平窗口] 到达，开始执行强平...")

    @classmethod
    def force_close(cls, wallet_name: str, side: str, price: float, result: str) -> None:
        """强平执行。"""
        symbol = cls.UP_SYMBOL if side == "UP" else cls.DOWN_SYMBOL
        status = "✓" if result == "成功" else "✗"
        print(f"   [强平] {wallet_name} {symbol} @ {price:.2f} {status}")

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

from api import get_wallet_usdc_balance
from notifications.feishu_tools import send_feishu
from state.reconcile_export import export_dual_wallet_event_to_excel

from strategy.event_task import EventTask
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
from strategy.order_executor_v2 import OrderExecutorV2, OrderOperationResult
from strategy.dual_wallet_models import (
    DualWalletEventState,
    EventOutcome,
    EventFlowState,
    LossWindowTracker,
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
    poll_interval_sec: float = 1.0
    entry_timeout_sec: int = 100
    force_close_window_sec: int = 60
    fixed_sell_price: float = 0.76
    max_consecutive_losses: int = 2
    min_seconds_before_start: int = 180  # 最小提前量检查（秒），需在事件开始前 N 秒挂单

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
            poll_interval_sec=float(config.get("dual_wallet_poll_interval_sec", 1.0)),
            entry_timeout_sec=int(config.get("dual_wallet_entry_timeout_sec", 100)),
            force_close_window_sec=int(config.get("dual_wallet_force_close_window_sec", 60)),
            fixed_sell_price=float(config.get("dual_wallet_fixed_sell_price", 0.76)),
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
    ):
        self.config = config
        self.executor = executor
        self.wallets = wallets
        self.run_folder = run_folder
        self.dry_run = dry_run
        self.structured_log = structured_log

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

        # 轮询器存储
        self._pollers: dict[str, Poller] = {}

        # 损失窗口跟踪
        self._loss_window = LossWindowTracker()
        self._halted = False
        self._halt_reason: str | None = None

        # 事件开始时的余额
        self._start_balances: dict[str, float | None] = {}

    # ===== 任务管理 =====

    def add_task(self, task: EventTask) -> EventTask:
        """添加新任务。"""
        self._tasks[task.event_id] = task

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
        """打印账户资金日志（迁移自原有 _log_wallet_account_data）。"""
        for wallet in self.wallets:
            try:
                payload = get_wallet_usdc_balance(account=wallet.account)
                balance = payload.get("balance_usdc") if isinstance(payload, dict) else None
                if isinstance(payload, dict) and payload.get("success") and balance is not None:
                    # 余额信息已在 _announce_event_start 中打印
                    pass
                else:
                    error = payload.get("error") if isinstance(payload, dict) else "balance_unavailable"
                    print(f"   ⚠️ {wallet.wallet_name}: 读取失败 | {error}")
            except Exception as e:
                print(f"   ⚠️ {wallet.wallet_name}: 读取失败 | {e}")

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
            # 跳过事件不打印详细日志，只在轮询结束时统一报告
            task.transition_to(EventTaskState.SKIPPED, f"时间不足")
            return

        # 事件已结束，直接进入结算
        if time_to_event_end <= 0:
            task.trigger_reason = "event_already_ended"
            task.trigger_detail = f"remaining_to_end_sec={int(time_to_event_end)};event_ended_before_wait"
            task.transition_to(EventTaskState.SETTLING_OUTCOME, "事件已结束")
            return

        # 事件未开始，等待（下次 tick 再检查）
        if start_time_utc and (start_time_utc - now).total_seconds() > 0:
            return

        # 事件已开始，执行前置检查
        amount_usd = task.metadata.get("amount_usd", 10.0)
        up_price = task.metadata.get("up_price", 0.5)
        down_price = task.metadata.get("down_price", 0.5)
        condition_id = task.condition_id

        # 使用新的终端进度风格日志
        PollTracker.start_event(task.event_name, time_to_event_start, end_time_utc.strftime("%H:%M:%S"))
        self._announce_event_start(task, time_to_event_start, amount_usd, up_price, down_price)
        TradeLogger.placing_orders()

        # 检查余额容量（跳过详细检查，只要能获取余额就继续）
        insufficient_wallets = []
        for wallet in self.wallets:
            try:
                payload = get_wallet_usdc_balance(account=wallet.account)
                balance = payload.get("balance_usdc") if isinstance(payload, dict) else None
                if not isinstance(payload, dict) or not payload.get("success") or balance is None:
                    insufficient_wallets.append(f"{wallet.wallet_name}:balance_unavailable")
            except Exception:
                insufficient_wallets.append(f"{wallet.wallet_name}:invalid_balance")

        if insufficient_wallets:
            TradeLogger.event_skip(f"余额检查失败: {', '.join(insufficient_wallets)}")
            task.transition_to(EventTaskState.STOPPED, f"余额检查失败")
            return

        # 记录 structured_log
        if self.structured_log:
            first_wallet_is_up = task.side_by_wallet_id.get(task.wallets[0].wallet_id) == OrderSide.UP if task.wallets else False
            self.structured_log.record_event(
                event_name=task.event_name,
                event_id=task.event_id,
                phase="start",
                payload={
                    "condition_id": condition_id,
                    "start_time": task.start_time.isoformat() if task.start_time else None,
                    "end_time": task.end_time.isoformat(),
                    "selected_accounts": self._build_selected_account_payload(),
                    "side_assignment": self._build_side_assignment_payload(task),
                    "random_assignment": {
                        "first_wallet_role": task.wallets[0].role.value if task.wallets else None,
                        "first_wallet_is_up": first_wallet_is_up,
                    },
                },
            )

        # 进入挂单阶段
        task.transition_to(EventTaskState.PLACING_ENTRY, "事件已开始，前置检查完成")

    def _process_placing_entry(self, task: EventTask) -> None:
        """处理 PLACING_ENTRY 状态 - 挂初始买单。"""
        up_price = task.metadata.get("up_price", 0.5)
        down_price = task.metadata.get("down_price", 0.5)
        amount_usd = task.metadata.get("amount_usd", 10.0)
        fee_rate_bps = task.metadata.get("fee_rate_bps", 0)

        for wallet in task.wallets:
            side = task.side_by_wallet_id.get(wallet.wallet_id)
            price = up_price if side == OrderSide.UP else down_price

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

            # 记录订单快照
            if result.snapshot:
                task.mark_order(result.snapshot)

            # 打印日志
            self._log_order_submission(wallet, side, amount_usd, result.snapshot)

            # 如果失败，打印详细错误
            if not result.outcome.success:
                print(f"      ⚠️ 挂单失败: {result.outcome.error}")

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
            return

        # 检查单边成交
        single_filled, filled_wallet_id, shares = task.check_single_side_filled()
        if single_filled:
            task.first_fill_wallet_id = filled_wallet_id
            task.trigger_reason = "single_side_filled"
            task.transition_to(EventTaskState.HANDLING_SINGLE, f"单边成交: {filled_wallet_id}")
            return

        # 检查 deadline 是否到达
        if deadline and now >= deadline:
            remaining_to_end = (end_time_utc - now).total_seconds()
            if remaining_to_end <= close_window_sec:
                task.trigger_reason = "force_close_window"
                print(f"   [超时进入] 强平窗口")
                task.transition_to(EventTaskState.WAITING_CLOSE_WINDOW, "进入强平窗口")
            else:
                task.trigger_reason = "entry_timeout"
                print(f"   [超时] 等待成交超时，进入结算")
                task.transition_to(EventTaskState.SETTLING_OUTCOME, "等待超时")
            return

        # 检查是否进入强平窗口
        remaining_to_end = (end_time_utc - now).total_seconds()
        if remaining_to_end <= close_window_sec:
            task.trigger_reason = "force_close_window"
            print(f"   [进入] 强平窗口")
            task.transition_to(EventTaskState.WAITING_CLOSE_WINDOW, "进入强平窗口")
            return

        # 每隔一段时间打印等待状态
        if hasattr(task, '_last_wait_log'):
            log_interval = 30  # 每 30 秒打印一次
            if now.timestamp() - task._last_wait_log > log_interval:
                print(f"   [等待] 成交中... (剩余 {remaining_to_end:.0f}s)")
                task._last_wait_log = now.timestamp()
        else:
            task._last_wait_log = now.timestamp()

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
        live_filled_shares = float(live_order.filled_shares or live_order.shares or 0.0) if live_order else 0.0
        live_filled_amount = float(live_order.filled_amount_usd or 0.0) if live_order else 0.0

        fee_rate_bps = task.metadata.get("fee_rate_bps", 0)

        # 挂 GTC 抛售单，不撤 stale 侧挂单
        sell_shares = live_filled_shares if live_filled_shares > 0 else (live_filled_amount / self.config.fixed_sell_price)

        sell_result = self._order_exec.place_gtc_sell_order(
            wallet=live_wallet,
            event_name=task.event_name,
            side=live_side,
            shares=sell_shares,
            price=self.config.fixed_sell_price,
            clob_token_ids=task.clob_token_ids,
            fee_rate_bps=fee_rate_bps,
            condition_id=task.condition_id,
        )

        # 记录抛售单快照
        if sell_result.snapshot:
            sell_result.snapshot.status = OrderStatus.SUBMITTED.value if sell_result.outcome.success else OrderStatus.FAILED.value
            task.mark_order(sell_result.snapshot)

        TradeLogger.single_filled(live_wallet.wallet_name, live_side.value if live_side else "?", live_filled_shares)
        TradeLogger.sell_order_placed(live_wallet.wallet_name, live_side.value if live_side else "?", self.config.fixed_sell_price, sell_shares)

        task.trigger_reason = "single_side_fill_pending_close"
        task.transition_to(EventTaskState.WAITING_CLOSE_WINDOW, "单边处理完成，等待强平窗口")
        print("   [单边] 已挂抛售单，等待强平窗口...")

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

        stale_order = task.get_order(stale_wallet.wallet_id)
        live_order = task.get_order(live_wallet.wallet_id)

        # 检查 1: stale 侧原始挂单是否已成交
        if stale_order and stale_order.status == OrderStatus.FILLED.value:
            print("\n   [双边处理] 未成交侧先成交，取消抛售单")
            # 取消 GTC 抛售单
            if live_order and live_order.order_id:
                cancel_result = self._order_exec.cancel_order(live_order.order_id, live_wallet)
                if cancel_result.snapshot:
                    cancel_result.snapshot.status = OrderStatus.CANCELLED.value
                    task.mark_order(cancel_result.snapshot)
                TradeLogger.order_cancelled(live_wallet.wallet_name, "抛售单")
            task.up_filled_shares = task.get_order(up_wallet.wallet_id).filled_shares if task.get_order(up_wallet.wallet_id) else 0.0
            task.down_filled_shares = task.get_order(down_wallet.wallet_id).filled_shares if task.get_order(down_wallet.wallet_id) else 0.0
            task.trigger_reason = "stale_order_filled_first"
            task.transition_to(EventTaskState.SETTLING_OUTCOME, "stale侧挂单先成交")
            return

        # 检查 2: GTC 抛售单是否成交
        if live_order and live_order.status == OrderStatus.FILLED.value:
            print("\n   [双边处理] 抛售单先成交，取消未成交侧")
            # 取消 stale 侧挂单
            if stale_order and stale_order.order_id:
                cancel_result = self._order_exec.cancel_order(stale_order.order_id, stale_wallet)
                if cancel_result.snapshot:
                    cancel_result.snapshot.status = OrderStatus.CANCELLED.value
                    task.mark_order(cancel_result.snapshot)
                TradeLogger.order_cancelled(stale_wallet.wallet_name, "未成交侧")
            task.up_filled_shares = task.get_order(up_wallet.wallet_id).filled_shares if task.get_order(up_wallet.wallet_id) else 0.0
            task.down_filled_shares = task.get_order(down_wallet.wallet_id).filled_shares if task.get_order(down_wallet.wallet_id) else 0.0
            task.trigger_reason = "sell_order_filled_first"
            task.transition_to(EventTaskState.SETTLING_OUTCOME, "抛售单先成交")
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
        """处理 SETTLING_OUTCOME 状态 - 轮询市场结果。"""
        poller_id = f"{task.event_id}_outcome"

        if poller_id not in self._pollers:
            self._pollers[poller_id] = create_outcome_poller(
                condition_id=task.condition_id,
                timeout_sec=self.config.outcome_poll_timeout_sec,
                poll_interval_sec=float(self.config.outcome_poll_interval_sec),
                on_progress=self._handle_poller_progress,
            )
            self._pollers[poller_id].start()
            print("   [轮询] 等待市场结果...")

        poller = self._pollers[poller_id]
        result = poller.poll()

        if result.is_complete:
            task.outcome = result.value
            del self._pollers[poller_id]
            outcome_str = "YES" if task.outcome == EventOutcome.YES else "NO" if task.outcome == EventOutcome.NO else str(task.outcome)
            print(f"   [结果] {outcome_str}")
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

    def _refresh_order_statuses(self, task: EventTask) -> None:
        """刷新订单状态。"""
        for wallet in task.wallets:
            snapshot = task.get_order(wallet.wallet_id)
            if not snapshot or not snapshot.order_id:
                continue
            if snapshot.status != OrderStatus.SUBMITTED.value:
                continue
            if snapshot.operation not in {OperationType.PLACE, OperationType.SELL}:
                continue

            outcome, updated_snapshot = self._order_exec.refresh_order_status(
                order_id=snapshot.order_id,
                wallet=wallet,
            )

            if not outcome.success:
                continue

            raw = outcome.raw if isinstance(outcome.raw, dict) else {}
            status = str(raw.get("status") or "").lower()

            # 归一化状态
            normalized_status = {
                "live": OrderStatus.SUBMITTED.value,
                "open": OrderStatus.SUBMITTED.value,
                "pending": OrderStatus.SUBMITTED.value,
                "matched": OrderStatus.FILLED.value,
                "filled": OrderStatus.FILLED.value,
                "executed": OrderStatus.FILLED.value,
                "cancelled": OrderStatus.CANCELLED.value,
                "canceled": OrderStatus.CANCELLED.value,
                "failed": OrderStatus.FAILED.value,
                "rejected": OrderStatus.FAILED.value,
            }.get(status, status)

            if normalized_status not in {OrderStatus.SUBMITTED.value, OrderStatus.FILLED.value, OrderStatus.CANCELLED.value, OrderStatus.FAILED.value}:
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
                        entry_filled_by_wallet[wallet.wallet_id] = float(snap.filled_shares or snap.shares or 0.0)
                        break

        # 检查 GTC 抛售单是否已被市场吃掉
        sell_order_filled_by_wallet: dict[str, bool] = {}
        for wallet in task.wallets:
            sell_order_filled_by_wallet[wallet.wallet_id] = any(
                snap.operation == OperationType.SELL and snap.status == OrderStatus.FILLED.value
                for snap in task.get_order_history(wallet.wallet_id)
            )

        print(f"\n⚡ 开始执行强平: {task.event_name} | 窗口={close_window_sec}s | 触发={task.trigger_reason}")

        fee_rate_bps = task.metadata.get("fee_rate_bps", 0)

        for wallet in task.wallets:
            order = task.get_order(wallet.wallet_id)
            wallet_name = wallet.wallet_name

            # 如果 GTC 抛售单已被市场吃掉，跳过强平
            if is_single_side_pending and sell_order_filled_by_wallet.get(wallet.wallet_id, False):
                print(f"   ⏭️ {wallet_name}: 抛售单已成交，跳过")
                continue

            # 尝试撤单
            cancel_already_terminal = False
            if order and order.order_id and order.status == OrderStatus.SUBMITTED.value:
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

                # 撤单后检查实际状态
                refresh_outcome, refresh_snapshot = self._order_exec.refresh_order_status(
                    order_id=order.order_id,
                    wallet=wallet,
                )
                if refresh_outcome.success and isinstance(refresh_outcome.raw, dict):
                    latest_status = str(refresh_outcome.raw.get("status") or "").lower()
                    if latest_status in {"matched", "filled", "executed"}:
                        order.status = OrderStatus.FILLED.value
                        task.mark_order(order)
                        print(f"   ⚠️ {wallet_name}: 撤单时发现已成交")
                        cancel_already_terminal = True
                    elif latest_status in {"cancelled", "canceled"}:
                        order.status = OrderStatus.CANCELLED.value
                        task.mark_order(order)
                        cancel_already_terminal = True
                    elif not cancel_outcome.success:
                        print(f"   ⚠️ {wallet_name}: 撤单失败，订单仍在簿上")
                        cancel_already_terminal = True

            if cancel_already_terminal:
                continue

            # 决定 FAK 平仓份额
            if is_single_side_pending and wallet.wallet_id in entry_filled_by_wallet:
                filled_shares = entry_filled_by_wallet[wallet.wallet_id]
            else:
                filled_shares = float(order.filled_shares or order.shares or 0.0) if order else 0.0

            if filled_shares <= 0:
                print(f"   ⏭️ {wallet_name}: 成交份额=0，跳过")
                continue

            # 执行 FAK 强平
            side = order.side if order else OrderSide.UP
            close_result = self._order_exec.execute_force_close(
                wallet=wallet,
                event_name=task.event_name,
                side=side,
                shares=filled_shares,
                clob_token_ids=task.clob_token_ids,
                fee_rate_bps=fee_rate_bps,
                condition_id=task.condition_id,
            )

            # 记录强平快照
            if close_result.snapshot:
                close_result.snapshot.status = OrderStatus.FILLED.value if close_result.outcome.success else OrderStatus.FAILED.value
                task.mark_order(close_result.snapshot)

            # 打印日志
            close_amount = filled_shares * self.config.fixed_sell_price
            result_str = "成功" if close_result.outcome.success else "失败"
            TradeLogger.force_close(task.event_name, wallet_name, side.value, f"${close_amount:.2f}", result_str)

    # ===== 完成处理 =====

    def _finalize_task(self, task: EventTask) -> None:
        """完成任务的最终处理。"""
        # 跳过的任务不打印结果、也不导出（没有实际交易）
        if task.state == EventTaskState.SKIPPED:
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
        side_text = side.value if side else "UNKNOWN"
        price = snapshot.price if snapshot and snapshot.price else 0.0
        error_text = snapshot.error if snapshot else ""

        if error_text:
            TradeLogger.order_failed(wallet.wallet_name, error_text)
        else:
            TradeLogger.order_submitted(wallet.wallet_name, side_text, float(price))

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

        # 发送飞书通知（只在真正交易时发送）
        if self.config.enable_feishu and self._has_actual_trades(task):
            try:
                # 格式化飞书消息
                emoji = "✅" if task.is_profit else "❌"
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
                    title=f"{emoji} 事件结束 | {task.event_name}",
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
