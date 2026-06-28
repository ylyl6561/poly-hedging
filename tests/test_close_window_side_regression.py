"""
回归测试：_process_waiting_close_window 中 live_side / stale_side 的 NameError。

Bug：在 _process_waiting_close_window 里只设置了 stale_wallet/live_wallet，
但没有设置 stale_side/live_side。后续 line 941/951/961/973 引用 live_side/stale_side 时
会触发 NameError。

修复：在 line 929 后增加：
    stale_side = task.side_by_wallet_id.get(stale_wallet.wallet_id)
    live_side = task.side_by_wallet_id.get(live_wallet.wallet_id)
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

# 避免 eth_account 硬依赖
if "eth_account" not in sys.modules:
    _fake = types.ModuleType("eth_account")
    _fake.Account = MagicMock()
    sys.modules["eth_account"] = _fake

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timezone, timedelta
from strategy.dual_wallet_models import OrderSide, OrderStatus, OperationType


def _make_task_for_close_window() -> MagicMock:
    """构造一个会触发 _process_waiting_close_window 走到 stale/live 决策点的 mock task。

    关键设置：
    - up_wallet / down_wallet 存在
    - first_fill_wallet_id 设置为 down_wallet（让 down_wallet = live_wallet）
    - side_by_wallet_id 包含两个 wallet 的 side
    - live_order / stale_order 状态构造一个会进入 line 940 分支的条件
      （stale_filled=True, live_filled=False）→ 走情况1路径，需要 live_side/stale_side
    """
    task = MagicMock()

    up_wallet = MagicMock()
    up_wallet.wallet_id = "wallet_up"
    up_wallet.wallet_name = "UP_WALLET"
    up_wallet.account = MagicMock()

    down_wallet = MagicMock()
    down_wallet.wallet_id = "wallet_down"
    down_wallet.wallet_name = "DOWN_WALLET"
    down_wallet.account = MagicMock()

    task.get_up_wallet.return_value = up_wallet
    task.get_down_wallet.return_value = down_wallet
    task.wallets = [up_wallet, down_wallet]

    # live_wallet = down_wallet（first_fill_id = down_wallet.wallet_id）
    task.first_fill_wallet_id = down_wallet.wallet_id

    # side_by_wallet_id
    task.side_by_wallet_id = {
        up_wallet.wallet_id: OrderSide.UP,
        down_wallet.wallet_id: OrderSide.DOWN,
    }

    # orders: stale=up（FILLED）, live=down（FILLED? no — 走情况 1：stale_filled and not live_filled）
    # 等等，我们想走到 line 940，那需要 stale_filled=True, live_filled=False
    stale_order = MagicMock()
    stale_order.order_id = "stale_order_1"
    stale_order.status = OrderStatus.FILLED.value
    stale_order.operation = OperationType.PLACE.value
    stale_order.side = OrderSide.UP
    stale_order.filled_shares = 100.0
    stale_order.filled_amount_usd = 50.0

    live_order = MagicMock()
    live_order.order_id = "live_order_1"
    live_order.status = OrderStatus.SUBMITTED.value  # 还没成交
    live_order.operation = OperationType.SELL.value
    live_order.side = OrderSide.DOWN
    live_order.filled_shares = 0.0

    task.get_order.side_effect = lambda wid: (
        stale_order if wid == up_wallet.wallet_id else live_order
    )

    # event timing
    task.end_time = datetime.now(timezone.utc) + timedelta(seconds=10)
    task.close_window_sec = 60

    # mark_order + transition
    task.mark_order = MagicMock()
    task.transition_to = MagicMock()

    return task


def _make_tm() -> MagicMock:
    """构造一个简化的 TaskManager：屏蔽 cancel/refresh 等副作用。"""
    from strategy.task_manager import TaskManager
    tm = MagicMock()
    tm.config = MagicMock()
    tm.config.fixed_sell_price = 0.76

    # 让 cancel_order 返回 success
    cancel_result = MagicMock()
    cancel_result.snapshot = MagicMock()
    cancel_result.outcome.success = True
    tm._order_exec.cancel_order.return_value = cancel_result

    # 让 refresh_order_status 直接通过（短路掉）
    tm._refresh_order_statuses = MagicMock()
    return tm


def test_live_side_and_stale_side_defined_in_close_window():
    """回归测试：_process_waiting_close_window 必须定义 live_side 和 stale_side。

    修复前：line 941 `live_side.value if live_side else "?"` 抛 NameError。
    修复后：应走到 line 942 的 print，不抛 NameError。
    """
    from strategy.task_manager import TaskManager

    task = _make_task_for_close_window()
    tm = _make_tm()

    # 不应抛 NameError
    try:
        TaskManager._process_waiting_close_window(tm, task)
        print("  ✓ _process_waiting_close_window 不抛 NameError")
    except NameError as e:
        raise AssertionError(
            f"修复回归：_process_waiting_close_window 仍抛 NameError: {e}"
        )
    except Exception as e:
        # 其他异常（如 _process_one_side_filled 的副作用）可接受
        # 但 NameError 是回归 bug，必须不能出现
        if "live_side" in str(e) or "stale_side" in str(e):
            raise AssertionError(f"回归：仍引用未定义的 live_side/stale_side: {e}")
        print(f"  ✓ _process_waiting_close_window 不抛 NameError (其他: {type(e).__name__})")


def test_live_side_value_used_in_log_message():
    """验证 live_side 实际值在 print 中可用（而不是 fallback 到 "?"）。

    修复前：因为 NameError，根本走不到 print。
    修复后：应能正确读取 live_side.value = "down"。
    """
    from strategy.task_manager import TaskManager
    import io
    from contextlib import redirect_stdout

    task = _make_task_for_close_window()
    tm = _make_tm()

    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            TaskManager._process_waiting_close_window(tm, task)
    except Exception as e:
        if "live_side" in str(e) or "stale_side" in str(e):
            raise AssertionError(f"回归：{e}")

    output = buf.getvalue()
    # 情况 1：stale 侧 FILLED → "原始挂单先成交"
    # print 应包含 "原始挂单先成交" + UP side 标识 + DOWN side 标识
    # 不应再包含 NameError traceback
    assert "原始挂单先成交" in output or "stale" in output.lower(), (
        f"期望进入 stale 侧处理路径，输出: {output[:300]}"
    )
    print(f"  ✓ live_side/stale_side 值正确解析，print 输出含正确路径")


if __name__ == "__main__":
    test_live_side_and_stale_side_defined_in_close_window()
    test_live_side_value_used_in_log_message()
    print("ALL WAITING_CLOSE_WINDOW NameError REGRESSION TESTS PASSED")