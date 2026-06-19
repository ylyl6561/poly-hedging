"""
Event Task State Machine - 新架构的核心状态定义。

这个模块定义了事件任务的完整生命周期状态，以及状态之间的转换规则。
与原有的 EventFlowState 不同，这里我们定义了更细粒度的状态来支持非阻塞并发执行。
"""

from __future__ import annotations

from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional


class EventTaskState(str, Enum):
    """
    事件任务的完整生命周期状态。

    每个状态都有明确的进入条件和退出条件，状态转换由 TaskManager 统一调度。
    """
    # 初始状态
    PENDING = "pending"           # 等待事件开始（距开始还有一定时间）

    # 挂单阶段
    PLACING_ENTRY = "placing_entry"  # 正在挂初始买单

    # 等待成交阶段
    WAITING_ENTRY = "waiting_entry"  # 等待初始挂单成交

    # 单边成交处理
    HANDLING_SINGLE = "handling_single"  # 单边成交，正在处理（撤单+挂抛售）

    # 强平阶段
    WAITING_CLOSE_WINDOW = "waiting_close_window"  # 等待进入强平窗口
    FORCE_CLOSING = "force_closing"  # 正在执行强平

    # 结算阶段
    SETTLING_OUTCOME = "settling_outcome"  # 轮询市场结果
    SETTLING_BALANCE = "settling_balance"  # 轮询余额稳定

    # 终止状态
    SETTLED = "settled"   # 已完成结算
    FAILED = "failed"     # 失败
    SKIPPED = "skipped"    # 跳过（如事件已结束）


# 状态分类
STATE_CATEGORIES = {
    "active": {
        EventTaskState.PENDING,
        EventTaskState.PLACING_ENTRY,
        EventTaskState.WAITING_ENTRY,
        EventTaskState.HANDLING_SINGLE,
        EventTaskState.WAITING_CLOSE_WINDOW,
        EventTaskState.FORCE_CLOSING,
        EventTaskState.SETTLING_OUTCOME,
        EventTaskState.SETTLING_BALANCE,
    },
    "terminal": {
        EventTaskState.SETTLED,
        EventTaskState.FAILED,
        EventTaskState.SKIPPED,
    },
}


def is_active_state(state: EventTaskState) -> bool:
    """判断是否为活跃状态（非终止状态）。"""
    return state in STATE_CATEGORIES["active"]


def is_terminal_state(state: EventTaskState) -> bool:
    """判断是否为终止状态。"""
    return state in STATE_CATEGORIES["terminal"]


# 状态转移规则
STATE_TRANSITIONS: dict[EventTaskState, set[EventTaskState]] = {
    # 初始状态可以转到任何状态
    EventTaskState.PENDING: {
        EventTaskState.PLACING_ENTRY,
        EventTaskState.SETTLING_OUTCOME,  # 事件已结束，直接结算
        EventTaskState.SKIPPED,           # 被跳过（如不合条件）
    },
    EventTaskState.PLACING_ENTRY: {
        EventTaskState.WAITING_ENTRY,
        EventTaskState.SETTLED,           # 失败后直接结算（无成交）
        EventTaskState.FAILED,
    },
    EventTaskState.WAITING_ENTRY: {
        EventTaskState.SETTLING_OUTCOME,  # 双边成交确认
        EventTaskState.HANDLING_SINGLE,   # 单边成交
        EventTaskState.WAITING_CLOSE_WINDOW,  # 等待超时，进入强平窗口
        EventTaskState.SETTLED,           # 等待超时但无成交
        EventTaskState.FAILED,
    },
    EventTaskState.HANDLING_SINGLE: {
        EventTaskState.WAITING_CLOSE_WINDOW,  # 处理完成，等待强平窗口
        EventTaskState.FORCE_CLOSING,     # 直接进入强平
        EventTaskState.SETTLING_OUTCOME,   # 事件已结束
    },
    EventTaskState.WAITING_CLOSE_WINDOW: {
        EventTaskState.FORCE_CLOSING,     # 进入强平窗口
        EventTaskState.SETTLING_OUTCOME,   # 事件已结束
        EventTaskState.SETTLED,           # 异常情况
    },
    EventTaskState.FORCE_CLOSING: {
        EventTaskState.SETTLING_OUTCOME,
        EventTaskState.SETTLED,           # 强平完成
        EventTaskState.FAILED,
    },
    EventTaskState.SETTLING_OUTCOME: {
        EventTaskState.SETTLING_BALANCE,  # 市场结果已出
        EventTaskState.SETTLED,           # 超时未出结果
        EventTaskState.FAILED,
    },
    EventTaskState.SETTLING_BALANCE: {
        EventTaskState.SETTLED,           # 余额稳定
        EventTaskState.FAILED,            # 超时
    },
    # 终止状态不可转移
    EventTaskState.SETTLED: set(),
    EventTaskState.FAILED: set(),
    EventTaskState.SKIPPED: set(),
}


def can_transition(from_state: EventTaskState, to_state: EventTaskState) -> bool:
    """检查状态转移是否合法。"""
    return to_state in STATE_TRANSITIONS.get(from_state, set())


@dataclass
class StateTransition:
    """状态转移记录。"""
    from_state: EventTaskState
    to_state: EventTaskState
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    reason: str = ""


# 状态元数据 - 描述每个状态的显示名称和优先级
STATE_METADATA: dict[EventTaskState, dict] = {
    EventTaskState.PENDING: {
        "name": "等待开始",
        "priority": 0,
        "color": "gray",
    },
    EventTaskState.PLACING_ENTRY: {
        "name": "挂单中",
        "priority": 10,
        "color": "blue",
    },
    EventTaskState.WAITING_ENTRY: {
        "name": "等待成交",
        "priority": 20,
        "color": "yellow",
    },
    EventTaskState.HANDLING_SINGLE: {
        "name": "单边处理",
        "priority": 25,
        "color": "orange",
    },
    EventTaskState.WAITING_CLOSE_WINDOW: {
        "name": "等待强平",
        "priority": 30,
        "color": "purple",
    },
    EventTaskState.FORCE_CLOSING: {
        "name": "强平中",
        "priority": 40,
        "color": "red",
    },
    EventTaskState.SETTLING_OUTCOME: {
        "name": "结算-等待结果",
        "priority": 50,
        "color": "cyan",
    },
    EventTaskState.SETTLING_BALANCE: {
        "name": "结算-等待余额",
        "priority": 60,
        "color": "green",
    },
    EventTaskState.SETTLED: {
        "name": "已完成",
        "priority": 100,
        "color": "white",
    },
    EventTaskState.FAILED: {
        "name": "失败",
        "priority": 100,
        "color": "red",
    },
    EventTaskState.SKIPPED: {
        "name": "已跳过",
        "priority": 100,
        "color": "gray",
    },
}


def get_state_display_name(state: EventTaskState) -> str:
    """获取状态的显示名称。"""
    return STATE_METADATA.get(state, {}).get("name", state.value)


def get_state_priority(state: EventTaskState) -> int:
    """获取状态的优先级（用于日志排序）。"""
    return STATE_METADATA.get(state, {}).get("priority", 50)
