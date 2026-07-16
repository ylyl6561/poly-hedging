"""
事件任务 (EventTask) - 状态机中的单个事件节点。

EventTask 代表一个事件在状态机中的完整生命周期。它包含：
1. 事件元数据
2. 当前状态
3. 状态轮询器
4. 业务逻辑的执行结果

每个 EventTask 由 TaskManager 统一调度，通过 tick() 方法驱动状态机前进。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from strategy.event_task_state import (
    EventTaskState,
    StateTransition,
    is_active_state,
    is_terminal_state,
    can_transition,
    get_state_display_name,
)
from strategy.dual_wallet_models import (
    EventOutcome,
    OrderSide,
    OrderSnapshot,
    WalletIdentity,
)


def coalesce_filled_shares(filled_shares: Any, target_shares: Any) -> float:
    """从 filled_shares / shares 两个字段中选出 fill 实际成交份额。

    关键区别 vs `filled_shares or target_shares`：
    - `or` 会把 0 当成 falsy，0 or X → X（吞掉真实的"成交为 0"）
    - 这里把 None（"SDK 没填"）和 0（"SDK 实际回填成交为 0"）严格区分：
      filled_shares is not None → 用 filled_shares（即便它是 0）
      否则 → 用 target_shares（snapshot.shares 是 entry 阶段写入的"挂单目标量"）

    边界：若 filled_shares 是无效字符串（如 "garbage"），视为"没有有效 fill 报告"，
    降级用 target_shares（语义同 None）。与 None 行为保持一致。

    单元测试：tests/test_coalesce_filled.py
    """
    if filled_shares is not None:
        try:
            return float(filled_shares)
        except (TypeError, ValueError):
            # 无效字符串 → 等同 None → fallback 到 target
            pass
    if target_shares is None:
        return 0.0
    try:
        return float(target_shares)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class EventTask:
    """
    事件任务 - 状态机中的单个事件节点。

    属性：
    - event_name: 事件名称
    - condition_id: Polymarket 条件 ID
    - clob_token_ids: YES/NO token IDs
    - end_time: 事件结束时间
    - wallets: 参与的钱包列表
    - side_by_wallet_id: 每个钱包对应的交易方向

    状态流转：
    PENDING → PLACING_ENTRY → WAITING_ENTRY → (HANDLING_SINGLE | WAITING_CLOSE_WINDOW) → FORCE_CLOSING → SETTLING_OUTCOME → SETTLING_BALANCE → SETTLED
    """

    # 事件标识
    event_name: str
    event_id: str
    condition_id: str
    clob_token_ids: list[str]
    end_time: datetime
    start_time: datetime | None = None
    close_window_sec: int = 60
    # Window duration label (e.g. "5m" / "15m" / "1h").  When ``None`` the
    # task is treated as the historical "5m" default.  Downstream consumers
    # use this to look up per-window overrides via
    # ``core.config.resolve_window_overrides``.
    window: str | None = None

    # 钱包配置
    wallets: list[WalletIdentity] = field(default_factory=list)
    side_by_wallet_id: dict[str, OrderSide] = field(default_factory=dict)

    # 状态
    state: EventTaskState = EventTaskState.PENDING
    state_transitions: list[StateTransition] = field(default_factory=list)

    # 订单历史
    order_snapshots: dict[str, list[OrderSnapshot]] = field(default_factory=dict)
    current_orders: dict[str, OrderSnapshot] = field(default_factory=dict)

    # 成交信息
    up_filled_shares: float = 0.0
    down_filled_shares: float = 0.0
    first_fill_wallet_id: str | None = None
    second_fill_wallet_id: str | None = None

    # 触发原因
    trigger_reason: str | None = None
    trigger_detail: str | None = None

    # 结算结果
    outcome: EventOutcome = EventOutcome.UNKNOWN
    outcome_payload: dict | None = None
    final_balances: dict[str, float | None] = field(default_factory=dict)
    pnl_by_wallet: dict[str, float] = field(default_factory=dict)
    total_pnl: float = 0.0
    is_profit: bool = False

    # 时间戳
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    state_changed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_poll_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_log_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # 元数据
    metadata: dict[str, Any] = field(default_factory=dict)

    # 外部回调
    on_state_change: Callable[[EventTask, EventTaskState, EventTaskState], None] | None = None
    on_progress: Callable[[EventTask], None] | None = None
    on_complete: Callable[[EventTask], None] | None = None

    def __post_init__(self):
        """初始化后处理。"""
        if self.end_time.tzinfo is None:
            self.end_time = self.end_time.astimezone(timezone.utc)
        if self.start_time and self.start_time.tzinfo is None:
            self.start_time = self.start_time.astimezone(timezone.utc)

    # ===== 状态转移 =====

    def transition_to(self, new_state: EventTaskState, reason: str = "") -> bool:
        """
        尝试转移到新状态。

        返回 True 表示转移成功，False 表示转移不合法。
        """
        if not can_transition(self.state, new_state):
            return False

        old_state = self.state
        self.state = new_state
        self.state_changed_at = datetime.now(timezone.utc)

        transition = StateTransition(
            from_state=old_state,
            to_state=new_state,
            reason=reason,
        )
        self.state_transitions.append(transition)

        # 触发回调
        if self.on_state_change:
            self.on_state_change(self, old_state, new_state)

        return True

    # ===== 时间计算 =====

    def remaining_to_start(self) -> float:
        """距事件开始的剩余时间（秒）。"""
        if self.start_time is None:
            return 0.0
        now = datetime.now(timezone.utc)
        return (self.start_time - now).total_seconds()

    def remaining_to_end(self) -> float:
        """距事件结束的剩余时间（秒）。"""
        now = datetime.now(timezone.utc)
        return (self.end_time - now).total_seconds()

    def is_event_ended(self) -> bool:
        """事件是否已结束。"""
        return self.remaining_to_end() <= 0

    def is_event_started(self) -> bool:
        """事件是否已开始。"""
        if self.start_time is None:
            return True
        return self.remaining_to_start() <= 0

    # ===== 订单操作 =====

    def mark_order(self, snapshot: OrderSnapshot) -> None:
        """记录订单快照。"""
        wallet_id = snapshot.wallet.wallet_id
        if wallet_id not in self.order_snapshots:
            self.order_snapshots[wallet_id] = []
        self.order_snapshots[wallet_id].append(snapshot)
        self.current_orders[wallet_id] = snapshot

    def get_order(self, wallet_id: str) -> OrderSnapshot | None:
        """获取当前订单。"""
        return self.current_orders.get(wallet_id)

    def get_order_history(self, wallet_id: str) -> list[OrderSnapshot]:
        """获取订单历史。"""
        return list(self.order_snapshots.get(wallet_id, []))
    def get_up_wallet(self) -> WalletIdentity | None:
        """获取 UP 方向的钱包。"""
        for wallet in self.wallets:
            if self.side_by_wallet_id.get(wallet.wallet_id) == OrderSide.UP:
                return wallet
        return None

    def get_down_wallet(self) -> WalletIdentity | None:
        """获取 DOWN 方向的钱包。"""
        for wallet in self.wallets:
            if self.side_by_wallet_id.get(wallet.wallet_id) == OrderSide.DOWN:
                return wallet
        return None

    def check_both_sides_filled(self) -> tuple[bool, float, float]:
        """检查双边是否都已成交。返回 (is_both_filled, up_shares, down_shares)。"""
        up_wallet = self.get_up_wallet()
        down_wallet = self.get_down_wallet()

        up_filled = False
        down_filled = False
        up_shares = 0.0
        down_shares = 0.0

        if up_wallet:
            up_order = self.get_order(up_wallet.wallet_id)
            if up_order and up_order.status == "filled":
                up_filled = True
                up_shares = coalesce_filled_shares(up_order.filled_shares, up_order.shares)

        if down_wallet:
            down_order = self.get_order(down_wallet.wallet_id)
            if down_order and down_order.status == "filled":
                down_filled = True
                down_shares = coalesce_filled_shares(down_order.filled_shares, down_order.shares)

        return (up_filled and down_filled, up_shares, down_shares)

    def check_single_side_filled(self) -> tuple[bool, str | None, float]:
        """检查是否有单边成交。返回 (is_single_filled, filled_wallet_id, shares)。"""
        up_wallet = self.get_up_wallet()
        down_wallet = self.get_down_wallet()

        for wallet, side in [(up_wallet, OrderSide.UP), (down_wallet, OrderSide.DOWN)]:
            if wallet is None:
                continue
            order = self.get_order(wallet.wallet_id)
            if order and order.status == "filled":
                shares = coalesce_filled_shares(order.filled_shares, order.shares)
                if shares > 0:
                    return (True, wallet.wallet_id, shares)

        return (False, None, 0.0)

    # ===== 日志和进度 =====

    def should_log_progress(self, interval_sec: float = 30.0) -> bool:
        """检查是否应该打印进度日志。"""
        now = datetime.now(timezone.utc)
        return (now - self.last_log_at).total_seconds() >= interval_sec

    def log_progress(self) -> None:
        """打印进度日志。"""
        self.last_log_at = datetime.now(timezone.utc)
        remaining_end = self.remaining_to_end()
        state_name = get_state_display_name(self.state)

        print(
            f"【{state_name}】{self.event_name}："
            f"状态={self.state.value} | "
            f"距结束={remaining_end:.0f}s | "
            f"UP份额={self.up_filled_shares:.4f} | "
            f"DOWN份额={self.down_filled_shares:.4f} | "
            f"触发={self.trigger_reason or '无'}"
        )

    # ===== 状态查询 =====

    @property
    def is_active(self) -> bool:
        """是否为活跃状态。"""
        return is_active_state(self.state)

    @property
    def is_terminal(self) -> bool:
        """是否为终止状态。"""
        return is_terminal_state(self.state)

    @property
    def is_settled(self) -> bool:
        """是否已结算。"""
        return self.state == EventTaskState.SETTLED

    @property
    def is_failed(self) -> bool:
        """是否失败。"""
        return self.state == EventTaskState.FAILED

    @property
    def age_sec(self) -> float:
        """任务创建以来的时间（秒）。"""
        return (datetime.now(timezone.utc) - self.created_at).total_seconds()

    @property
    def state_age_sec(self) -> float:
        """当前状态持续时间（秒）。"""
        return (datetime.now(timezone.utc) - self.state_changed_at).total_seconds()

    # ===== 摘要 =====

    def get_summary(self) -> dict:
        """获取任务摘要。"""
        return {
            "event_name": self.event_name,
            "event_id": self.event_id,
            "state": self.state.value,
            "state_display": get_state_display_name(self.state),
            "outcome": self.outcome.value,
            "total_pnl": self.total_pnl,
            "is_profit": self.is_profit,
            "remaining_to_end": round(self.remaining_to_end(), 1),
            "state_age_sec": round(self.state_age_sec, 1),
            "up_filled_shares": self.up_filled_shares,
            "down_filled_shares": self.down_filled_shares,
            "trigger_reason": self.trigger_reason,
            "window": self.window,
        }

    def __repr__(self) -> str:
        return (
            f"EventTask(name={self.event_name!r}, window={self.window!r}, "
            f"state={self.state.value}, remaining_to_end={self.remaining_to_end():.0f}s)"
        )
