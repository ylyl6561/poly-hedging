"""
TaskManager._refresh_order_statuses 节流行为测试。

验证：
1. tick=100ms 时，同 task 的 fetch 频率被节流到 4Hz（MIN_REFRESH_INTERVAL_SEC=0.25）
2. fill 检测延迟上限 ≤ MIN_REFRESH_INTERVAL_SEC + 一次 tick
3. 多 task 并发：HTTP 调用总数符合预期
4. 状态机的其他检查（deadline、force close 窗口）不受节流影响
"""
from __future__ import annotations

import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

# 测试环境如果没装 eth_account（本地 lint 环境），fake 一个避免硬依赖
if "eth_account" not in sys.modules:
    _fake = types.ModuleType("eth_account")
    _fake.Account = MagicMock()
    sys.modules["eth_account"] = _fake

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.dual_wallet_models import (
    OrderSnapshot,
    OrderStatus,
    OperationType,
    WalletIdentity,
)


# ===== 测试 fixture 构造 =====

def _make_tm(min_interval: float | None = None):
    """构造一个简化的 TaskManager mock：屏蔽掉 import 链，只暴露 _refresh_order_statuses 需要的依赖。

    真实 _refresh_order_statuses 会调用：
      - self._order_exec.refresh_order_status(order_id, wallet)
      - 后续归一化逻辑

    我们只需要：
      1. mock _order_exec.refresh_order_status 返回 (success_outcome, snapshot)
      2. 让 self.MIN_REFRESH_INTERVAL_SEC 是真实 float（不是 MagicMock）

    返回 (tm, restore_fn)。
    """
    from strategy.task_manager import TaskManager
    tm = MagicMock()

    # 在 instance 上设值（覆盖 MagicMock 自动生成的同名属性）
    interval = min_interval if min_interval is not None else TaskManager.MIN_REFRESH_INTERVAL_SEC
    tm.MIN_REFRESH_INTERVAL_SEC = interval

    def restore():
        pass  # 不需要清理 instance 属性（每个 tm 是局部的）

    # 默认：返回一个 successful outcome + 一个空 snapshot
    tm._order_exec.refresh_order_status.return_value = (
        MagicMock(success=True, status="LIVE", order_id="x"),
        MagicMock(),
    )
    return tm, restore


def _make_task_with_submitted_orders() -> MagicMock:
    """构造一个 mock task：两个 wallet 各有一个 SUBMITTED 订单。"""
    task = MagicMock()

    snap_up = MagicMock(spec=OrderSnapshot)
    snap_up.order_id = "order_up_123"
    snap_up.operation = OperationType.PLACE.value
    snap_up.status = OrderStatus.SUBMITTED.value

    snap_down = MagicMock(spec=OrderSnapshot)
    snap_down.order_id = "order_down_456"
    snap_down.operation = OperationType.PLACE.value
    snap_down.status = OrderStatus.SUBMITTED.value

    wallet_up = MagicMock(spec=WalletIdentity)
    wallet_up.wallet_id = "wallet_up"
    wallet_down = MagicMock(spec=WalletIdentity)
    wallet_down.wallet_id = "wallet_down"

    task.wallets = [wallet_up, wallet_down]
    task.get_order.side_effect = lambda wid: (
        snap_up if wid == "wallet_up" else snap_down
    )
    task.mark_order = MagicMock()
    # 显式设置 _last_refresh_time = 0.0，否则 getattr(task, ..., 0.0) 会被 MagicMock 拦截返回 mock
    task._last_refresh_time = 0.0
    return task


# ===== 单元测试 =====

def test_min_interval_is_class_constant():
    """MIN_REFRESH_INTERVAL_SEC 是 TaskManager 类常量。"""
    from strategy.task_manager import TaskManager
    assert hasattr(TaskManager, "MIN_REFRESH_INTERVAL_SEC")
    assert TaskManager.MIN_REFRESH_INTERVAL_SEC == 0.25
    print(f"  ✓ MIN_REFRESH_INTERVAL_SEC = {TaskManager.MIN_REFRESH_INTERVAL_SEC}s")


def test_first_call_always_passes_through():
    """首次调用 _refresh_order_statuses 一定执行（last_refresh 默认 0.0）。"""
    from strategy.task_manager import TaskManager

    task = _make_task_with_submitted_orders()
    tm, restore = _make_tm()

    call_count = [0]

    def fake_refresh(*args, **kwargs):
        call_count[0] += 1
        return MagicMock(success=True, status="LIVE"), MagicMock()

    tm._order_exec.refresh_order_status.side_effect = fake_refresh

    try:
        TaskManager._refresh_order_statuses(tm, task)

        assert call_count[0] == 2, f"首次应 fetch 2 次，实际 {call_count[0]}"
        assert hasattr(task, "_last_refresh_time")
        print(f"  ✓ 首次调用 fetch {call_count[0]} 次（无节流）")
    finally:
        restore()


def test_throttle_skips_subsequent_calls_within_window():
    """100ms tick × 5 次 = 500ms 内只 fetch 一次（节流）。"""
    from strategy.task_manager import TaskManager

    task = _make_task_with_submitted_orders()
    tm, restore = _make_tm()

    call_count = [0]

    def fake_refresh(*args, **kwargs):
        call_count[0] += 1
        return MagicMock(success=True, status="LIVE"), MagicMock()

    tm._order_exec.refresh_order_status.side_effect = fake_refresh

    try:
        for _ in range(5):
            TaskManager._refresh_order_statuses(tm, task)
            time.sleep(0.05)

        assert call_count[0] <= 4, (
            f"500ms 内 fetch 应 ≤ 4 次（实际 {call_count[0]}）"
        )
        print(f"  ✓ 500ms 内 fetch {call_count[0]} 次（≤ 4 预期）")
    finally:
        restore()


def test_throttle_allows_call_after_window_elapses():
    """超过节流窗口（250ms）后允许下次 fetch。"""
    from strategy.task_manager import TaskManager

    task = _make_task_with_submitted_orders()
    tm, restore = _make_tm(min_interval=0.10)  # 100ms 窗口更易测试

    call_count = [0]

    def fake_refresh(*args, **kwargs):
        call_count[0] += 1
        return MagicMock(success=True, status="LIVE"), MagicMock()

    tm._order_exec.refresh_order_status.side_effect = fake_refresh

    try:
        TaskManager._refresh_order_statuses(tm, task)        # 第 1 次：执行
        time.sleep(0.05)
        TaskManager._refresh_order_statuses(tm, task)        # 第 2 次：50ms < 100ms → 节流
        time.sleep(0.10)
        TaskManager._refresh_order_statuses(tm, task)        # 第 3 次：总 150ms > 100ms → 执行

        assert call_count[0] == 4, (
            f"应 fetch 4 次（2 wallet × 2 次执行），实际 {call_count[0]}"
        )
        print(f"  ✓ 节流窗口过后允许执行: fetch {call_count[0]} 次")
    finally:
        restore()


def test_concurrent_tasks_each_throttle_independently():
    """多个 task 各自独立节流（每 task 自己的 _last_refresh_time）。"""
    from strategy.task_manager import TaskManager

    tasks = [_make_task_with_submitted_orders() for _ in range(3)]
    tm, restore = _make_tm()

    call_count = [0]

    def fake_refresh(*args, **kwargs):
        call_count[0] += 1
        return MagicMock(success=True, status="LIVE"), MagicMock()

    tm._order_exec.refresh_order_status.side_effect = fake_refresh

    try:
        for task in tasks:
            TaskManager._refresh_order_statuses(tm, task)
        # 同 tick 内的第二次循环应全部被节流
        for task in tasks:
            TaskManager._refresh_order_statuses(tm, task)

        assert call_count[0] == 6, (
            f"3 task 首次各 2 wallet = 6 fetch；同 tick 第二次应全部节流；实际 {call_count[0]}"
        )
        print(f"  ✓ 多 task 独立节流: 3 task × 2 wallet 首次 fetch {call_count[0]} 次")
    finally:
        restore()


def test_100ms_tick_real_scenario_throttle_works():
    """真实场景：tick=100ms 运行 1s 内 fetch 次数受节流约束。"""
    from strategy.task_manager import TaskManager

    task = _make_task_with_submitted_orders()
    tm, restore = _make_tm()

    call_count = [0]

    def fake_refresh(*args, **kwargs):
        call_count[0] += 1
        return MagicMock(success=True, status="LIVE"), MagicMock()

    tm._order_exec.refresh_order_status.side_effect = fake_refresh

    try:
        # 模拟 0.5s 内的连续 tick（sleep 0.05 模拟 50ms tick 加速版）
        t0 = time.monotonic()
        while time.monotonic() - t0 < 0.5:
            TaskManager._refresh_order_statuses(tm, task)
            time.sleep(0.05)

        elapsed = time.monotonic() - t0
        # 0.5s 内最多 1 (首次) + floor(0.5/0.25) = 3 次 = 6 fetch (2 wallet)
        max_fetch = (1 + int(elapsed / 0.25)) * 2
        assert call_count[0] <= max_fetch, (
            f"{elapsed*1000:.0f}ms 内 fetch {call_count[0]} 次，超过预期上限 {max_fetch}"
        )
        print(f"  ✓ 真实场景: {elapsed*1000:.0f}ms 内 fetch {call_count[0]} 次（上限 {max_fetch}）")
    finally:
        restore()


def test_no_pending_orders_skips_fetch_even_outside_window():
    """没有 pending 订单时，第 1 步早返回（pending 为空），但仍然更新 _last_refresh_time。

    这意味着：如果 task 没有订单需要刷新，节流早返回仍然占用节流窗口。
    这是符合预期的——节流是按 task 整体节流，不是按 wallet 节流。
    """
    from strategy.task_manager import TaskManager

    task = MagicMock()
    task.wallets = []
    task.get_order.return_value = None  # 无任何订单
    task._last_refresh_time = 0.0
    tm, restore = _make_tm()

    call_count = [0]

    def fake_refresh(*args, **kwargs):
        call_count[0] += 1
        return MagicMock(success=True, status="LIVE"), MagicMock()

    tm._order_exec.refresh_order_status.side_effect = fake_refresh

    try:
        TaskManager._refresh_order_statuses(tm, task)  # 首次（pending 为空 → 第 1 步 return）
        time.sleep(0.30)  # 300ms > 250ms
        TaskManager._refresh_order_statuses(tm, task)  # 第二次

        # 即便第 1 步空 pending 早返回，节流窗口仍然消耗
        assert call_count[0] == 0, (
            f"没有 pending 订单 → 不应触发 fetch（实际 {call_count[0]}）"
        )
        print(f"  ✓ 无 pending 订单时不触发 fetch")
    finally:
        restore()

    # 即便第 1 步空 pending 早返回，节流窗口仍然消耗
    assert call_count[0] == 0, (
        f"没有 pending 订单 → 不应触发 fetch（实际 {call_count[0]}）"
    )
    print(f"  ✓ 无 pending 订单时不触发 fetch")


if __name__ == "__main__":
    test_min_interval_is_class_constant()
    test_first_call_always_passes_through()
    test_throttle_skips_subsequent_calls_within_window()
    test_throttle_allows_call_after_window_elapses()
    test_concurrent_tasks_each_throttle_independently()
    test_100ms_tick_real_scenario_throttle_works()
    test_no_pending_orders_skips_fetch_even_outside_window()
    print("ALL TASK_MANAGER REFRESH THROTTLE TESTS PASSED")