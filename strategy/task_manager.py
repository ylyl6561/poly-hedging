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
    min_seconds_before_start: int = 15  # 最小提前量检查

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
        print(f"【事件】{task.event_name}")
        print(f"【窗口】{task.start_time.astimezone(timezone.utc).isoformat()} UTC -> {task.end_time.astimezone(timezone.utc).isoformat()} UTC")
        print(f"【市场】condition_id={condition_id} | close_price={self.config.fixed_sell_price:.4f} | timeout={self.config.entry_timeout_sec}s")
        print(f"【下单门槛】距开始至少 {self.config.min_seconds_before_start}s | 当前剩余 {task.remaining_to_start():.1f}s")
        print(f"【初始挂单配置】amount_usd={float(amount_usd):.4f} | up_price={float(up_price):.4f} | down_price={float(down_price):.4f}")
        selected_accounts = [f"{wallet.wallet_name}({wallet.account.account_id})" for wallet in self.wallets]
        print(f"【选中账号】{' | '.join(selected_accounts)}")
        mapping = []
        for wallet in self.wallets:
            side = task.side_by_wallet_id.get(wallet.wallet_id)
            mapping.append(f"{wallet.wallet_name}({wallet.account.account_id})->{side.value if side else 'UNKNOWN'}")
        print(f"【本轮分配】{' | '.join(mapping)}")

    def _log_wallet_account_data(self, amount_usd: float) -> None:
        """打印账户资金日志（迁移自原有 _log_wallet_account_data）。"""
        for wallet in self.wallets:
            try:
                payload = get_wallet_usdc_balance(account=wallet.account)
                balance = payload.get("balance_usdc") if isinstance(payload, dict) else None
                if isinstance(payload, dict) and payload.get("success") and balance is not None:
                    print(f"【账户资金】{wallet.wallet_name}：available_usdc={float(balance):.4f} | next_order_amount={float(amount_usd):.4f}")
                else:
                    error = payload.get("error") if isinstance(payload, dict) else "balance_unavailable"
                    print(f"【账户资金】{wallet.wallet_name}：读取失败 | error={error}")
            except Exception as e:
                print(f"【账户资金】{wallet.wallet_name}：读取失败 | error={e}")

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
            mode = "dry_run" if self.dry_run else "live"
            self._halt_reason = f"insufficient_{mode}_balance | {'; '.join(insufficient_wallets)}"
            if not self.dry_run:
                self._halted = True
                print(f"【停机】{self._halt_reason}")
            else:
                print(f"【跳过】{self._halt_reason}")
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
            print(
                f"【跳过】{task.event_name}：距离开始仅剩 {time_to_event_start:.1f}s，"
                f"小于最小提前量 {self.config.min_seconds_before_start}s，不执行挂单"
            )
            task.transition_to(EventTaskState.SKIPPED, f"距离开始时间不足: {time_to_event_start:.1f}s < {self.config.min_seconds_before_start}s")
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

        # 打印事件公告
        self._announce_event(task, condition_id=condition_id, amount_usd=amount_usd, up_price=up_price, down_price=down_price)

        # 打印账户资金
        self._log_wallet_account_data(amount_usd=amount_usd)

        # 检查余额容量
        if not self._ensure_wallet_capacity(task, amount_usd=amount_usd):
            task.transition_to(EventTaskState.STOPPED, f"余额不足: {self._halt_reason}")
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

        print(f"【挂单】{task.event_name}：开始挂初始买单")

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

        task.trigger_reason = "entry_placed"
        task.transition_to(EventTaskState.WAITING_ENTRY, "挂单完成")

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
                task.transition_to(EventTaskState.WAITING_CLOSE_WINDOW, "进入强平窗口")
            else:
                task.trigger_reason = "entry_timeout"
                task.transition_to(EventTaskState.SETTLING_OUTCOME, "等待超时")
            return

        # 检查是否进入强平窗口
        remaining_to_end = (end_time_utc - now).total_seconds()
        if remaining_to_end <= close_window_sec:
            task.trigger_reason = "force_close_window"
            task.transition_to(EventTaskState.WAITING_CLOSE_WINDOW, "进入强平窗口")

    def _process_handling_single(self, task: EventTask) -> None:
        """处理 HANDLING_SINGLE 状态 - 单边成交处理（撤单 + 挂抛售单）。"""
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

        # 获取 stale 侧的订单信息
        stale_order = task.get_order(stale_wallet.wallet_id)
        stale_order_id = stale_order.order_id if stale_order else None
        stale_amount = float(stale_order.amount_usd or 0.0) if stale_order else 0.0

        fee_rate_bps = task.metadata.get("fee_rate_bps", 0)

        print(f"【单边成交处理】{task.event_name}：{live_side.value} 侧成交，开始撤单 + 挂抛售单")

        # 执行单边成交处理
        cancel_result, cancel_refresh_result, sell_result = self._order_exec.handle_single_side_fill(
            wallet=live_wallet,
            event_name=task.event_name,
            side=live_side,
            filled_shares=live_filled_shares,
            filled_amount_usd=live_filled_amount,
            stale_wallet=stale_wallet,
            stale_side=stale_side,
            stale_order_id=stale_order_id,
            stale_amount_usd=stale_amount,
            clob_token_ids=task.clob_token_ids,
            fee_rate_bps=fee_rate_bps,
            condition_id=task.condition_id,
        )

        # 记录撤单快照
        if cancel_result.snapshot:
            cancel_result.snapshot.status = OrderStatus.CANCELLED.value if cancel_result.outcome.success else OrderStatus.FAILED.value
            task.mark_order(cancel_result.snapshot)

        # 检查撤单后刷新结果
        if cancel_refresh_result.outcome.success and cancel_refresh_result.snapshot:
            # 订单实际已成交，更新状态
            stale_snapshot = task.get_order(stale_wallet.wallet_id)
            if stale_snapshot:
                stale_snapshot.status = OrderStatus.FILLED.value
                task.mark_order(stale_snapshot)
            print(f"【单边成交处理-撤单回查】{task.event_name}：撤单API失败但回查发现 stale 侧实际已成交，按双边成交处理")

        # 记录抛售单快照
        if sell_result.snapshot:
            sell_result.snapshot.status = OrderStatus.SUBMITTED.value if sell_result.outcome.success else OrderStatus.FAILED.value
            task.mark_order(sell_result.snapshot)

        # 打印详细日志
        live_account = f"{live_wallet.wallet_name}({live_wallet.account.account_id})"
        stale_account = f"{stale_wallet.wallet_name}({stale_wallet.account.account_id})"
        sell_shares = live_filled_shares if live_filled_shares > 0 else (live_filled_amount / self.config.fixed_sell_price)
        
        print(
            f"【单边成交处理-账号动作】{task.event_name}："
            f"撤单账号={stale_account} | 撤单结果={'成功' if cancel_result.outcome.success else '失败'}"
            f" | 抛售账号={live_account} | 抛售价格={self.config.fixed_sell_price:.4f}"
            f" | 抛售份额={sell_shares:.4f} | 抛售结果={'成功' if sell_result.outcome.success else '失败'}"
            f" | 动作=仅撤单+挂 GTC 抛售单，FAK 强平延后到 end-{close_window_sec}s 窗口"
        )

        task.trigger_reason = "single_side_fill_pending_close"
        task.transition_to(EventTaskState.WAITING_CLOSE_WINDOW, "单边处理完成，等待强平窗口")

    def _process_waiting_close_window(self, task: EventTask) -> None:
        """处理 WAITING_CLOSE_WINDOW 状态 - 等待强平窗口。"""
        # 刷新订单状态（检查 GTC 抛售单是否已被市场吃掉）
        self._refresh_order_statuses(task)

        end_time_utc = task.end_time
        if end_time_utc.tzinfo is None:
            end_time_utc = end_time_utc.astimezone(timezone.utc)

        now = datetime.now(timezone.utc)
        remaining_to_end = (end_time_utc - now).total_seconds()
        close_window_sec = task.close_window_sec

        if remaining_to_end <= close_window_sec:
            task.transition_to(EventTaskState.FORCE_CLOSING, "强平窗口到达")
        elif remaining_to_end <= 0:
            task.transition_to(EventTaskState.SETTLING_OUTCOME, "事件已结束")

    def _process_force_closing(self, task: EventTask) -> None:
        """处理 FORCE_CLOSING 状态 - 执行强平。"""
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
            print(f"【结算-等待结果】{task.event_name}：轮询市场结果，超时 {self.config.outcome_poll_timeout_sec}s，间隔 {self.config.outcome_poll_interval_sec}s")

        poller = self._pollers[poller_id]
        result = poller.poll()

        if result.is_complete:
            task.outcome = result.value
            del self._pollers[poller_id]
            print(f"【结算确认】{task.event_name}：市场结果已揭晓: {task.outcome.value}")
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
            print(f"【结算-等待余额】{task.event_name}：等待余额稳定，超时 {self.config.settlement_poll_timeout_sec}s，间隔 {self.config.settlement_poll_interval_sec}s，需 {self.config.settlement_stable_rounds} 轮")

        poller = self._pollers[poller_id]
        result = poller.poll()

        if result.is_complete:
            task.final_balances = result.value
            del self._pollers[poller_id]
            print(f"【结算完成】{task.event_name}：余额已稳定，进入结算")
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

        print(
            f"【{close_window_sec}秒窗口强平】{task.event_name}：开始执行强平"
            f" | 账号={' | '.join(selected_accounts)}"
            f" | 触发原因={task.trigger_reason}"
        )

        fee_rate_bps = task.metadata.get("fee_rate_bps", 0)

        for wallet in task.wallets:
            order = task.get_order(wallet.wallet_id)
            account_text = f"{wallet.wallet_name}({wallet.account.account_id})"

            # 如果 GTC 抛售单已被市场吃掉，跳过强平
            if is_single_side_pending and sell_order_filled_by_wallet.get(wallet.wallet_id, False):
                print(f"【强平-跳过】{task.event_name}：账号={account_text} | GTC 抛售单已被市场吃掉，跳过 FAK")
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

                print(f"【强平-撤单】{task.event_name}：账号={account_text} | 结果={'成功' if cancel_outcome.success else '失败'}")

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
                        print(f"【强平-校验】{task.event_name}：账号={account_text} | 撤单后重查发现已成交，跳过 FAK")
                        cancel_already_terminal = True
                    elif latest_status in {"cancelled", "canceled"}:
                        order.status = OrderStatus.CANCELLED.value
                        task.mark_order(order)
                        cancel_already_terminal = True
                    elif not cancel_outcome.success:
                        print(f"【强平-校验】{task.event_name}：账号={account_text} | 撤单失败且订单仍在簿上，跳过 FAK")
                        cancel_already_terminal = True

            if cancel_already_terminal:
                continue

            # 决定 FAK 平仓份额
            if is_single_side_pending and wallet.wallet_id in entry_filled_by_wallet:
                filled_shares = entry_filled_by_wallet[wallet.wallet_id]
            else:
                filled_shares = float(order.filled_shares or order.shares or 0.0) if order else 0.0

            if filled_shares <= 0:
                print(f"【强平-跳过】{task.event_name}：账号={account_text} | 成交份额=0，跳过平仓")
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
            if close_result.outcome.success:
                print(
                    f"【强平-平仓】{task.event_name}：账号={account_text} | side={side.value}"
                    f" | 份额={filled_shares:.4f} | 金额={close_amount:.4f} | 结果=成功"
                )
            else:
                print(
                    f"【强平-平仓】{task.event_name}：账号={account_text} | 结果=失败 | error={close_result.outcome.error or 'unknown'}"
                )

    # ===== 完成处理 =====

    def _finalize_task(self, task: EventTask) -> None:
        """完成任务的最终处理。"""
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
            self._halt_reason = f"max_consecutive_losses:{consecutive_losses}/{self.config.max_consecutive_losses}"
            print(f"【停机】{self._halt_reason}")

        # 打印结果
        print(f"【事件结果】{task.event_name}：{'盈利' if task.is_profit else '亏损'}；总收益={task.total_pnl:.4f}")
        for wallet_id, pnl in task.pnl_by_wallet.items():
            balance = task.final_balances.get(wallet_id)
            balance_text = "UNKNOWN" if balance is None else f"{balance:.4f}"
            print(f"【钱包汇总】{wallet_id}：pnl={pnl:.4f} | available_usdc={balance_text}")

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
        print(f"【状态转移】{task.event_name}: {old_state.value} → {new_state.value}")

    def _handle_task_progress(self, task: EventTask) -> None:
        """处理任务进度日志。"""
        if task.should_log_progress(self.config.progress_log_interval_sec):
            remaining_end = task.remaining_to_end()
            state_name = get_state_display_name(task.state)
            print(
                f"【{state_name}】{task.event_name}："
                f"距结束={remaining_end:.0f}s | "
                f"UP份额={task.up_filled_shares:.4f} | "
                f"DOWN份额={task.down_filled_shares:.4f} | "
                f"触发={task.trigger_reason or '无'}"
            )

    def _handle_task_complete(self, task: EventTask) -> None:
        """处理任务完成。"""
        print(f"【任务完成】{task.event_name}: PnL={task.total_pnl:.4f}, 盈亏={'盈利' if task.is_profit else '亏损'}")

    def _handle_poller_progress(self, poller: Poller) -> None:
        """处理轮询器进度日志。"""
        status = poller.get_status()
        remaining = status.get("remaining_sec", 0)
        rounds = status.get("rounds", 0)
        print(f"【轮询进度】{poller.name}: 第 {rounds} 轮, 剩余 {remaining:.0f}s...")

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
        price_text = f"{snapshot.price:.4f}" if snapshot and snapshot.price else "UNKNOWN"
        shares_text = f"{snapshot.shares:.4f}" if snapshot and snapshot.shares else "UNKNOWN"
        status_text = snapshot.status if snapshot else "UNKNOWN"
        error_text = snapshot.error if snapshot else ""
        order_id_text = snapshot.order_id if snapshot else "UNKNOWN"

        error_detail = f" | error={error_text}" if error_text else ""
        print(
            f"【挂单】{wallet.wallet_name}({wallet.account.account_id})"
            f" | side={side_text}"
            f" | amount_usd={float(amount_usd):.4f}"
            f" | price={price_text}"
            f" | shares={shares_text}"
            f" | status={status_text}{error_detail}"
            f" | order_id={order_id_text}"
        )

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

        # 发送飞书通知
        if self.config.enable_feishu:
            try:
                wallet_lines = []
                for wallet_id, pnl in task.pnl_by_wallet.items():
                    balance = task.final_balances.get(wallet_id)
                    balance_text = "UNKNOWN" if balance is None else f"{balance:.4f}"
                    wallet_lines.append(f"{wallet_id}: pnl={pnl:.4f}, balance={balance_text}")

                send_feishu(
                    title=f"双钱包事件结果 | {task.event_name}",
                    content="\n".join([
                        f"事件: {task.event_name}",
                        f"最终结果: {task.outcome.value}",
                        f"总收益: {task.total_pnl:.4f}",
                        f"是否盈利: {'是' if task.is_profit else '否'}",
                        f"最近窗口连续亏损: {self._loss_window.consecutive_losses()} / {self.config.max_consecutive_losses}",
                    ] + wallet_lines),
                    level="success" if task.is_profit else "warn",
                )
            except Exception:
                pass

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
        print(f"【任务管理器】启动 | 活跃任务: {self.active_count}")
        while self.active_count > 0 and not self.should_halt():
            completed = self.tick()
            if completed:
                print(f"【任务管理器】完成 {len(completed)} 个任务 | 剩余活跃: {self.active_count}")
            time.sleep(self.config.poll_interval_sec)

        print(f"【任务管理器】停止 | 完成: {len(self._completed_tasks)} | 失败: {len(self._failed_tasks)}")
