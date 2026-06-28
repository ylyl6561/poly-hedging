"""
Dual-wallet event strategy orchestration.

这是新架构的入口类，封装了 TaskManager 来管理多个事件的并发执行。
原有基于 run_event 的串行阻塞实现已迁移到 strategy/task_manager.py。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from accounts import get_account_registry
from core import resolve_config
from state.structured_log import StructuredRunLog

from .account_pool import AccountPool
from .dual_wallet_executor import DualWalletExecutor
from .dual_wallet_models import (
    EventResultSummary,
    LossWindowTracker,
    build_wallet_identities,
)


class DualWalletEventStrategy:
    """
    双钱包事件策略（新架构）。

    使用 TaskManager 管理多个事件的并发执行。
    """

    def __init__(
        self,
        *,
        run_folder,
        dry_run: bool,
        config: dict[str, Any] | None = None,
        structured_log: StructuredRunLog | None = None,
        mock_mode: bool = False,
        mock_fill_side: str = "UP",
        mock_fill_after_sec: float = 5.0,
    ):
        self.run_folder = run_folder
        self.dry_run = dry_run
        self.config = config or resolve_config(__file__)
        self.structured_log = structured_log
        self.mock_mode = mock_mode
        self.mock_fill_side = mock_fill_side
        self.mock_fill_after_sec = mock_fill_after_sec

        if mock_mode:
            # 使用 Mock 执行器
            from .mock_order_executor import MockOrderExecutor, MockOrderConfig
            mock_config = MockOrderConfig(
                fill_side=mock_fill_side,
                fill_after_sec=mock_fill_after_sec,
                fill_immediately=False,
            )
            self.executor = MockOrderExecutor(mock_config)
            print(f"   [MOCK] 模式启动: fill_side={mock_fill_side}, fill_after={mock_fill_after_sec}s")
        else:
            self.executor = DualWalletExecutor(
                dry_run=dry_run,
                dry_run_status_script=self.config.get("dual_wallet_dry_run_status_script", {}),
            )

        registry = get_account_registry()
        self.account_pool = AccountPool(registry.list_accounts())
        self.selected_accounts = self._select_two_dual_wallet_accounts()
        self.wallets = build_wallet_identities(self.selected_accounts)
        self.loss_window = LossWindowTracker()
        self._halted = False
        self._halt_reason: str | None = None

    def should_halt(self) -> bool:
        """是否应该停机。"""
        return self._halted

    def halt_reason(self) -> str | None:
        """获取停机原因。"""
        return self._halt_reason

    def _select_two_dual_wallet_accounts(self):
        """选择两个用于双钱包策略的账号。"""
        tagged_accounts = self.account_pool.accounts_with_tag("dual_wallet")
        if len(tagged_accounts) >= 2:
            return tagged_accounts[:2]
        return self.account_pool.require_accounts(2)

    def create_task_manager(self) -> "TaskManager":
        """
        创建任务管理器。

        返回 TaskManager 实例，可以管理多个事件的并发执行。
        """
        from strategy.task_manager import TaskManager, TaskManagerConfig

        config = TaskManagerConfig.from_config_dict(self.config)
        return TaskManager(
            config=config,
            executor=self.executor,
            wallets=self.wallets,
            run_folder=self.run_folder,
            dry_run=self.dry_run,
            structured_log=self.structured_log,
        )

    def run_event(
        self,
        event_name: str,
        event_id: str,
        start_time,
        end_time,
        clob_token_ids: list[str],
        fee_rate_bps: int,
        condition_id: str,
        entry_shares: float,
        up_price: float,
        down_price: float,
        close_price: float = 0.0,
        close_window_sec: int = 60,
    ) -> EventResultSummary:
        """
        运行单个事件（串行阻塞模式）。

        创建 TaskManager 并运行单个事件，等待完成后返回结果。
        与 run_parallel 互补，run_parallel 用于批量并行执行。

        Args:
            event_name: 事件名称
            event_id: 事件 ID
            start_time: 开始时间
            end_time: 结束时间
            clob_token_ids: YES/NO token IDs
            fee_rate_bps: 费率（基点）
            condition_id: 条件 ID
            entry_shares: 每钱包的下单 token 数量（固定数量，配合 up/down 价格算出 amount_usd）
            up_price: UP 方向价格
            down_price: DOWN 方向价格
            close_price: 结算价格
            close_window_sec: 平仓窗口秒数

        Returns:
            EventResultSummary: 事件结果摘要
        """
        from strategy.event_task import EventTask
        from strategy.dual_wallet_models import assign_event_sides

        manager = self.create_task_manager()
        side_by_wallet_id, _ = assign_event_sides(self.wallets)

        # 规范化时间
        if start_time and start_time.tzinfo is None:
            start_time = start_time.astimezone(timezone.utc)
        if end_time and end_time.tzinfo is None:
            end_time = end_time.astimezone(timezone.utc)

        task = EventTask(
            event_name=event_name,
            event_id=event_id,
            condition_id=condition_id,
            clob_token_ids=clob_token_ids,
            start_time=start_time,
            end_time=end_time,
            close_window_sec=close_window_sec,
            wallets=self.wallets,
            side_by_wallet_id=side_by_wallet_id,
            metadata={
                "up_price": up_price,
                "down_price": down_price,
                "entry_shares": entry_shares,
                "fee_rate_bps": fee_rate_bps,
                "close_price": close_price,
            },
        )
        manager.add_task(task)
        manager.run()

        return manager._build_legacy_summary(task)

    def run_parallel(
        self,
        events: list[dict],
        max_concurrent: int = 10,
    ) -> list[EventResultSummary]:
        """
        并行运行多个事件。

        使用 TaskManager 统一调度多个事件，每个事件独立推进状态机。

        Args:
            events: 事件列表，每个事件包含:
                - event_name: 事件名称
                - condition_id: Polymarket 条件 ID
                - clob_token_ids: YES/NO token IDs
                - start_time: 事件开始时间 (datetime)
                - end_time: 事件结束时间 (datetime)
                - up_price: UP 方向价格 (可选，默认 0.5)
                - down_price: DOWN 方向价格 (可选，默认 0.5)
                - amount_usd: 挂单金额 (可选，默认 10.0)
                - fee_rate_bps: 费率 (可选，默认 0)
            max_concurrent: 最大并发数 (默认 10)

        Returns:
            结果摘要列表
        """
        from strategy.event_task import EventTask
        from strategy.dual_wallet_models import assign_event_sides

        manager = self.create_task_manager()

        # 添加所有事件
        for event_info in events[:max_concurrent]:
            side_by_wallet_id, _ = assign_event_sides(self.wallets)

            # 规范化时间
            start_time = event_info.get("start_time")
            end_time = event_info.get("end_time")
            if start_time and start_time.tzinfo is None:
                start_time = start_time.astimezone(timezone.utc)
            if end_time and end_time.tzinfo is None:
                end_time = end_time.astimezone(timezone.utc)

            task = EventTask(
                event_name=event_info.get("event_name", "unknown"),
                event_id=event_info.get("condition_id", ""),
                condition_id=event_info.get("condition_id", ""),
                clob_token_ids=event_info.get("clob_token_ids", []),
                end_time=end_time,
                start_time=start_time,
                close_window_sec=int(self.config.get("dual_wallet_force_close_window_sec", 60)),
                wallets=self.wallets,
                side_by_wallet_id=side_by_wallet_id,
                metadata={
                    "up_price": event_info.get("up_price", 0.5),
                    "down_price": event_info.get("down_price", 0.5),
                    "amount_usd": event_info.get("amount_usd", 10.0),
                    "fee_rate_bps": event_info.get("fee_rate_bps", 0),
                    "close_price": event_info.get("close_price", 0),
                },
            )
            manager.add_task(task)

        # 运行主循环
        manager.run()

        # 返回结果
        return [
            manager._build_legacy_summary(task)
            for task in manager.completed_tasks
        ]

    def get_status_summary(self) -> dict:
        """获取当前状态摘要。"""
        return {
            "halted": self._halted,
            "halt_reason": self._halt_reason,
            "wallets": [
                {
                    "wallet_id": w.wallet_id,
                    "wallet_name": w.wallet_name,
                    "role": w.role.value,
                    "account_id": w.account.account_id,
                }
                for w in self.wallets
            ],
        }
