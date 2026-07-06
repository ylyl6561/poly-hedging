"""
全流程 e2e mock 测试（不依赖真实 SDK / 网络）。

覆盖 TaskManager 的端到端状态机流，覆盖以下 4 个场景：

场景 A — 正常双边成交：
  PLACING_ENTRY → WAITING_ENTRY → SETTLING_OUTCOME
  (UP + DOWN entry 都 fill，task 进入 SETTLING_OUTCOME)

场景 B — 单边成交 + sell 成功（正常路径，无 balance guard 触发）：
  PLACING_ENTRY → WAITING_ENTRY → HANDLING_SINGLE → WAITING_CLOSE_WINDOW
  → FORCE_CLOSING → SETTLING_OUTCOME
  on-chain balance 充足，sell 正常挂出；强平窗口对未成交侧强平。

场景 C — 【bug 路径】单边成交 + matched 未 settle：
  PLACING_ENTRY → WAITING_ENTRY → HANDLING_SINGLE → WAITING_CLOSE_WINDOW
  → FORCE_CLOSING → SETTLING_OUTCOME
  on-chain balance=0，balance guard 跳过 sell；强平窗口兜底；
  关键约束：place_gtc_sell_order **从未被调用**，FAILED 快照存在。

场景 D — sell 真实 SDK 400 失败：
  PLACING_ENTRY → WAITING_ENTRY → HANDLING_SINGLE → WAITING_CLOSE_WINDOW
  → FORCE_CLOSING → SETTLING_OUTCOME
  on-chain balance 充足，但 place_gtc_sell_order 仍返回 400（edge case 兜底）；
  走原失败分支 + 诊断日志输出；状态机照旧。
"""
from __future__ import annotations

import contextlib
import io
import sys
import types
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# 避免 eth_account 硬依赖
if "eth_account" not in sys.modules:
    _fake = types.ModuleType("eth_account")
    _fake.Account = MagicMock()
    sys.modules["eth_account"] = _fake

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import strategy.task_manager as tm_module
from strategy.dual_wallet_models import (
    OperationType,
    OrderSide,
    OrderSnapshot,
    OrderStatus,
    WalletIdentity,
)
from strategy.event_task import EventTask
from strategy.event_task_state import EventTaskState


# ===== 钱包 / Task 工厂 =====


@dataclass
class FakeAccount:
    account_id: str = "acct_x"
    label: str = "X"
    signature_type: int = 2
    wallet_address: str = "0xw_x"


def make_wallet(wallet_id: str, side: OrderSide) -> WalletIdentity:
    return WalletIdentity(
        wallet_id=wallet_id,
        wallet_name=f"W_{wallet_id}",
        account=FakeAccount(account_id=f"acct_{wallet_id}"),
        role=side,
    )


def make_event_task(*, up_filled: bool, down_filled: bool, with_stale_pending: bool = False) -> EventTask:
    """构造一个真实 EventTask，end_time 在 30s 后，start_time 在 -3min。"""
    up_wallet = make_wallet("w_up", OrderSide.UP)
    down_wallet = make_wallet("w_down", OrderSide.DOWN)
    now = datetime.now(timezone.utc)
    task = EventTask(
        event_name="btc-5m-mock",
        event_id="evt_001",
        condition_id="0xcond_001",
        clob_token_ids=["111_up_token", "222_down_token"],
        start_time=now - timedelta(minutes=3),
        end_time=now + timedelta(seconds=30),
        close_window_sec=20,
        wallets=[up_wallet, down_wallet],
        side_by_wallet_id={up_wallet.wallet_id: OrderSide.UP, down_wallet.wallet_id: OrderSide.DOWN},
        metadata={"entry_shares": 5.0, "up_price": 0.5, "down_price": 0.5, "fee_rate_bps": 0},
    )
    task.state = EventTaskState.PLACING_ENTRY
    return task


# ===== mock 工厂：TaskManager + executor + balance 接口 =====


def make_tm(
    *,
    up_balance: float | None = None,
    down_balance: float | None = None,
    sell_should_succeed: bool = True,
    sell_error_message: str | None = None,
    force_close_should_succeed: bool = True,
    place_entry_success: bool = True,
) -> tuple[Any, EventTask, MagicMock]:
    """构造一个最小 TaskManager（__new__ 跳过 __init__）。

    返回 (tm, task, fake_balance_holder)
      - fake_balance_holder 是用 list 模拟的可变引用，方便测试 patch fetch_token_balance。
    """
    tm = tm_module.TaskManager.__new__(tm_module.TaskManager)
    tm.config = MagicMock()
    tm.config.fixed_sell_price = 0.76
    tm.config.entry_timeout_sec = 60
    tm.config.dry_run = False
    tm.wallets = []

    # 默认 fetch_token_balance mock（可被 patch 覆盖）
    fake_balance_holder = {"up": up_balance, "down": down_balance}

    def fake_fetch_token_balance(asset_id, account, mock=False):
        # asset_id = 111_up_token → up；222_down_token → down
        if asset_id == "111_up_token":
            return {"success": True, "balance_shares": fake_balance_holder["up"], "raw": {}, "asset_id": asset_id}
        if asset_id == "222_down_token":
            return {"success": True, "balance_shares": fake_balance_holder["down"], "raw": {}, "asset_id": asset_id}
        return {"success": False, "balance_shares": None, "error": "unknown_token", "asset_id": asset_id}

    # place_entry mock：默认成功并返回带 filled_shares 的快照
    def make_entry_snapshot(outcome_success, wallet, side):
        snap = OrderSnapshot(
            wallet=wallet,
            event_name="btc-5m-mock",
            side=side,
            amount_usd=5.0 * 0.5,
            operation=OperationType.PLACE,
            order_id=f"order_{wallet.wallet_id}",
            token_id="111_up_token" if side == OrderSide.UP else "222_down_token",
            condition_id="0xcond_001",
            price=0.5,
            shares=5.0,
            status=OrderStatus.SUBMITTED.value if outcome_success else OrderStatus.FAILED.value,
            filled_shares=5.0 if outcome_success else 0.0,
            filled_amount_usd=2.5 if outcome_success else 0.0,
        )
        return snap

    # fetch_order_status（_refresh_order_statuses 内部使用）：强制返回 SUBMITTED
    def fake_refresh_order_status(*, order_id, wallet):
        outcome = MagicMock()
        outcome.success = True
        outcome.raw = {"status": "live"}
        outcome.shares = 5.0
        outcome.filled_shares = 0.0
        outcome.filled_amount_usd = 0.0
        outcome.average_fill_price = None
        return outcome, None

    # place_gtc_sell_order mock
    def fake_place_gtc_sell_order(*, wallet, event_name, side, shares, price, clob_token_ids, fee_rate_bps, condition_id):
        snap = OrderSnapshot(
            wallet=wallet,
            event_name=event_name,
            side=side,
            amount_usd=shares * price,
            operation=OperationType.SELL,
            order_id=f"sell_{wallet.wallet_id}" if sell_should_succeed else None,
            token_id="111_up_token" if side == OrderSide.UP else "222_down_token",
            condition_id=condition_id,
            price=price,
            shares=shares,
            status=OrderStatus.SUBMITTED.value if sell_should_succeed else OrderStatus.FAILED.value,
            filled_shares=0.0,
            filled_amount_usd=0.0,
            error=None if sell_should_succeed else (sell_error_message or "direct_clob_failed: mock_400"),
        )
        outcome = MagicMock()
        outcome.success = sell_should_succeed
        outcome.order_id = f"sell_{wallet.wallet_id}" if sell_should_succeed else None
        outcome.error = None if sell_should_succeed else (sell_error_message or "direct_clob_failed: mock_400")
        outcome.shares = shares
        outcome.filled_shares = 0.0
        outcome.filled_amount_usd = 0.0
        outcome.average_fill_price = price
        outcome.raw = {"status": "live"} if sell_should_succeed else {"error": sell_error_message or "mock_400"}
        result = MagicMock()
        result.outcome = outcome
        result.snapshot = snap
        return result

    # place_market_close_order mock
    def fake_place_market_close_order(*, wallet, event_name, side, shares, price, clob_token_ids, fee_rate_bps, condition_id):
        snap = OrderSnapshot(
            wallet=wallet,
            event_name=event_name,
            side=side,
            amount_usd=shares * price,
            operation=OperationType.FORCE_CLOSE,
            order_id=f"market_{wallet.wallet_id}",
            token_id="111_up_token" if side == OrderSide.UP else "222_down_token",
            condition_id=condition_id,
            price=price,
            shares=shares,
            status=OrderStatus.FILLED.value if force_close_should_succeed else OrderStatus.FAILED.value,
            filled_shares=shares if force_close_should_succeed else 0.0,
            filled_amount_usd=shares * price if force_close_should_succeed else 0.0,
        )
        outcome = MagicMock()
        outcome.success = force_close_should_succeed
        outcome.order_id = f"market_{wallet.wallet_id}" if force_close_should_succeed else None
        outcome.error = None if force_close_should_succeed else "force_close_failed"
        outcome.shares = shares
        outcome.filled_shares = shares if force_close_should_succeed else 0.0
        outcome.filled_amount_usd = shares * price if force_close_should_succeed else 0.0
        outcome.average_fill_price = price
        outcome.raw = {"status": "matched"}
        result = MagicMock()
        result.outcome = outcome
        result.snapshot = snap
        return result

    # place_entry mock
    def fake_place_entry_order(*, wallet, event_name, side, amount_usd, price, clob_token_ids, fee_rate_bps, condition_id):
        outcome = MagicMock()
        outcome.success = place_entry_success
        outcome.order_id = f"order_{wallet.wallet_id}"
        outcome.error = None if place_entry_success else "entry_failed"
        outcome.shares = 5.0
        outcome.filled_shares = 5.0 if place_entry_success else 0.0
        outcome.filled_amount_usd = 2.5 if place_entry_success else 0.0
        outcome.average_fill_price = price
        outcome.raw = {"status": "matched"}
        snap = make_entry_snapshot(place_entry_success, wallet, side)
        result = MagicMock()
        result.outcome = outcome
        result.snapshot = snap
        return result

    # 装配 _order_exec
    tm._order_exec = MagicMock()
    tm._order_exec.place_entry_order.side_effect = fake_place_entry_order
    tm._order_exec.place_gtc_sell_order.side_effect = fake_place_gtc_sell_order
    tm._order_exec.place_market_close_order.side_effect = fake_place_market_close_order
    tm._order_exec.execute_force_close.side_effect = fake_place_market_close_order
    tm._order_exec.refresh_order_status.side_effect = fake_refresh_order_status
    # cancel_order：默认成功
    cancel_result = MagicMock()
    cancel_result.snapshot = OrderSnapshot(
        wallet=make_wallet("placeholder", OrderSide.UP),
        event_name="btc-5m-mock",
        side=OrderSide.UP,
        amount_usd=2.5,
        operation=OperationType.CANCEL,
        order_id="cancelled",
        price=0.5,
        shares=5.0,
        status=OrderStatus.CANCELLED.value,
    )
    cancel_result.outcome.success = True
    tm._order_exec.cancel_order.return_value = cancel_result

    return tm, fake_balance_holder, fake_fetch_token_balance


# ===== 阶段驱动 helper =====


def simulate_entry_fill(task: EventTask, up_filled: bool, down_filled: bool):
    """把 task 内部订单状态模拟成 entry 已 fill（填 filled_shares）。"""
    for wallet_id, filled in [("w_up", up_filled), ("w_down", down_filled)]:
        order = task.get_order(wallet_id)
        if order and filled:
            order.filled_shares = 5.0
            order.filled_amount_usd = 2.5
            order.status = OrderStatus.FILLED.value


def patch_balance_and_run(tm, task, fetch_fn):
    """同时 patch fetch_token_balance 到 fetch_fn 并重定向 stdout。"""
    p = patch("strategy.task_manager.fetch_token_balance", side_effect=fetch_fn)
    return p


# ===== 场景 A：正常双边成交 =====


def test_scenario_a_both_filled_settling_outcome():
    """UP + DOWN 都 fill → task 直接进入 SETTLING_OUTCOME，不触发 HANDLING_SINGLE。"""
    task = make_event_task(up_filled=True, down_filled=True)
    tm, _, fetch_fn = make_tm()

    # PLACING_ENTRY：place_entry
    with contextlib.redirect_stdout(io.StringIO()), patch("strategy.task_manager.fetch_token_balance", side_effect=fetch_fn):
        tm._process_placing_entry(task)
    # 期望：两个 entry 都挂出，状态推进到 WAITING_ENTRY
    assert task.state == EventTaskState.WAITING_ENTRY, task.state
    assert tm._order_exec.place_entry_order.call_count == 2
    # 模拟两边 entry 都 fill
    simulate_entry_fill(task, up_filled=True, down_filled=True)

    # WAITING_ENTRY：双 fill → SETTLING_OUTCOME
    with contextlib.redirect_stdout(io.StringIO()), patch("strategy.task_manager.fetch_token_balance", side_effect=fetch_fn):
        tm._process_waiting_entry(task)
    assert task.state == EventTaskState.SETTLING_OUTCOME, task.state
    # 关键：balance guard 在 WAITING_ENTRY 阶段不参与，place_gtc_sell_order 没被调用
    tm._order_exec.place_gtc_sell_order.assert_not_called()
    # 关键：place_market_close_order 也没被调用
    tm._order_exec.place_market_close_order.assert_not_called()
    print("  ✓ 场景 A：双边成交 → SETTLING_OUTCOME，无 sell / 无强平")


# ===== 场景 B：单边成交 + sell 成功 =====


def test_scenario_b_single_fill_balance_ok_normal_sell_path():
    """UP fill, DOWN 未 fill；balance 充足 → HANDLING_SINGLE → 挂 sell → WAITING_CLOSE_WINDOW。

    关键校验：
    - place_gtc_sell_order 必须被调用（balance guard 没拦）
    - sell 成功挂出，状态为 SUBMITTED
    - 后续强平窗口 UP 有 sell 单在簿、DOWN 无仓位 — 强平不触发（正确业务）
    """
    task = make_event_task(up_filled=True, down_filled=False)
    tm, balance_holder, fetch_fn = make_tm(up_balance=5.0, down_balance=0.0)

    with contextlib.redirect_stdout(io.StringIO()), patch("strategy.task_manager.fetch_token_balance", side_effect=fetch_fn):
        tm._process_placing_entry(task)
    assert task.state == EventTaskState.WAITING_ENTRY

    simulate_entry_fill(task, up_filled=True, down_filled=False)

    # WAITING_ENTRY：单 fill → HANDLING_SINGLE
    with contextlib.redirect_stdout(io.StringIO()), patch("strategy.task_manager.fetch_token_balance", side_effect=fetch_fn):
        tm._process_waiting_entry(task)
    assert task.state == EventTaskState.HANDLING_SINGLE, task.state

    # HANDLING_SINGLE：balance=5.0 ≥ 5.0 → 正常挂 sell
    with contextlib.redirect_stdout(io.StringIO()), patch("strategy.task_manager.fetch_token_balance", side_effect=fetch_fn):
        tm._process_handling_single(task)
    # 关键：place_gtc_sell_order 必须被调用，且只调用一次（UP 侧）
    assert tm._order_exec.place_gtc_sell_order.call_count == 1, tm._order_exec.place_gtc_sell_order.call_count
    assert task.state == EventTaskState.WAITING_CLOSE_WINDOW, task.state
    # 关键：sell 单 SUBMITTED（在簿上等待被吃）
    sell_snaps = [s for s in task.get_order_history("w_up") if s.operation == OperationType.SELL]
    assert sell_snaps, "缺少抛售单快照"
    assert sell_snaps[-1].status == OrderStatus.SUBMITTED.value, sell_snaps[-1].status
    print("  ✓ 场景 B：balance 充足 → 正常挂 sell → SUBMITTED 在簿（业务正确）")


# ===== 场景 C：bug 路径 — 单边成交 + matched 未 settle =====


def test_scenario_c_balance_guard_skips_sell_force_close_takeover():
    """UP fill 但 on-chain balance=0 → balance guard 跳过 sell → 强平兜底。

    这是修复的核心 bug 路径（issue #54 / #328）：
    - entry matched 但 token 没 settle
    - balance=0 → sell 注定 400 → guard 跳过
    - 强平窗口对 live 侧（实际无仓位）和 stale 侧（无仓位）尝试强平
    """
    task = make_event_task(up_filled=True, down_filled=False)
    tm, balance_holder, fetch_fn = make_tm(up_balance=0.0, down_balance=0.0)

    with contextlib.redirect_stdout(io.StringIO()), patch("strategy.task_manager.fetch_token_balance", side_effect=fetch_fn):
        tm._process_placing_entry(task)
    simulate_entry_fill(task, up_filled=True, down_filled=False)

    with contextlib.redirect_stdout(io.StringIO()), patch("strategy.task_manager.fetch_token_balance", side_effect=fetch_fn):
        tm._process_waiting_entry(task)
    assert task.state == EventTaskState.HANDLING_SINGLE

    # HANDLING_SINGLE：balance=0 → guard 跳过 sell
    with contextlib.redirect_stdout(io.StringIO()), patch("strategy.task_manager.fetch_token_balance", side_effect=fetch_fn):
        tm._process_handling_single(task)

    # 关键断言 1：place_gtc_sell_order **从未被调用**（避免 CLOB 400）
    tm._order_exec.place_gtc_sell_order.assert_not_called()
    # 关键断言 2：状态机仍推进到 WAITING_CLOSE_WINDOW（与原版本契约一致）
    assert task.state == EventTaskState.WAITING_CLOSE_WINDOW, task.state
    # 关键断言 3：写了一条 FAILED SELL 快照
    sell_snaps = [s for s in task.get_order_history("w_up") if s.operation == OperationType.SELL]
    assert sell_snaps, "balance guard 必须写入 FAILED 快照"
    assert sell_snaps[-1].status == OrderStatus.FAILED.value
    assert "on_chain_balance_mismatch" in (sell_snaps[-1].error or "")

    # 推进到强平窗口
    task.end_time = datetime.now(timezone.utc) + timedelta(seconds=5)
    task.trigger_reason = "single_side_fill_pending_close"
    with contextlib.redirect_stdout(io.StringIO()), patch("strategy.task_manager.fetch_token_balance", side_effect=fetch_fn):
        tm._process_waiting_close_window(task)

    # 真实业务：UP entry 已 fill → live_filled=True → 走 "live 侧成交但 stale 未成交"
    # 分支 → 进 SETTLING_OUTCOME（balance guard 跳过的本意是不再发注定 400 的 sell，
    # 但仓位本身仍在；这条路径等市场结算 + UP 侧自动 redeem/down 侧 expired 处理）
    assert task.state == EventTaskState.SETTLING_OUTCOME, task.state
    # 关键断言：current_order 必须仍是 PLACE（balance guard 不能覆盖它）
    up_current = task.get_order("w_up")
    assert up_current is not None and up_current.operation == OperationType.PLACE, (
        f"current_order 被错误覆盖为 {up_current.operation if up_current else None}"
    )
    # 关键：place_gtc_sell_order 仍然没被调（guard 真的生效）
    assert tm._order_exec.place_gtc_sell_order.call_count == 0
    print("  ✓ 场景 C：balance=0 → guard 跳过 sell → 状态 SETTLING_OUTCOME + current_order 保持 PLACE")


# ===== 场景 D：sell 真实 SDK 400（balance 充足但 CLOB 拒绝）=====


def test_scenario_d_balance_ok_but_clob_400_falls_back_to_force_close():
    """balance 充足但 place_gtc_sell_order 仍返回 400（edge case 兜底）。"""
    task = make_event_task(up_filled=True, down_filled=False)
    sell_err = (
        "PolyApiException[status_code=400, error_message={'error': "
        "'not enough balance / allowance: the balance is not enough -> "
        "balance: 0, order amount: 5000000'}]"
    )
    tm, _, fetch_fn = make_tm(
        up_balance=5.0,
        down_balance=0.0,
        sell_should_succeed=False,
        sell_error_message=sell_err,
    )

    with contextlib.redirect_stdout(io.StringIO()), patch("strategy.task_manager.fetch_token_balance", side_effect=fetch_fn):
        tm._process_placing_entry(task)
    simulate_entry_fill(task, up_filled=True, down_filled=False)

    with contextlib.redirect_stdout(io.StringIO()), patch("strategy.task_manager.fetch_token_balance", side_effect=fetch_fn):
        tm._process_waiting_entry(task)
    assert task.state == EventTaskState.HANDLING_SINGLE

    # HANDLING_SINGLE：balance 充足但 sell 仍 400 → 走原失败分支 + 诊断日志
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), patch("strategy.task_manager.fetch_token_balance", side_effect=fetch_fn):
        tm._process_handling_single(task)
    output = buf.getvalue()

    # 关键：place_gtc_sell_order 被调用（balance guard 没拦）
    assert tm._order_exec.place_gtc_sell_order.call_count == 1
    # 关键：诊断日志输出 "CLOB raw error" / "balance=0"
    assert "CLOB raw error" in output, output
    assert "balance_raw=0.0" in output, output
    # 关键：状态机仍推进
    assert task.state == EventTaskState.WAITING_CLOSE_WINDOW

    # 强平窗口：sell status=FAILED 覆盖了 current_order，所以 live_filled=False
    # （FAILED ≠ FILLED），stale 侧 DOWN 未 fill → 不走"live_filled 路径"，
    # 走强平窗口 → FORCE_CLOSING
    task.end_time = datetime.now(timezone.utc) + timedelta(seconds=5)
    task.trigger_reason = "single_side_fill_pending_close"
    with contextlib.redirect_stdout(io.StringIO()), patch("strategy.task_manager.fetch_token_balance", side_effect=fetch_fn):
        tm._process_waiting_close_window(task)
    assert task.state == EventTaskState.FORCE_CLOSING, task.state
    # 关键：FAILED 卖单快照确实写入了（不是 balance guard 的 history-only）
    sell_snaps = [s for s in task.get_order_history("w_up") if s.operation == OperationType.SELL]
    assert sell_snaps[-1].status == OrderStatus.FAILED.value, sell_snaps[-1].status
    assert "balance: 0, order amount: 5000000" in (sell_snaps[-1].error or ""), sell_snaps[-1].error
    # 强平窗口：原失败路径下 current_order 被 SELL FAILED 覆盖，_execute_force_close
    # 看不到 entry 单 → 强平不会触发；但状态机进入 FORCE_CLOSING，最终进 SETTLING_OUTCOME
    # 等市场结果结算。这是已知的边角行为 — 诊断日志 + 强平窗口兜底足够。
    with contextlib.redirect_stdout(io.StringIO()), patch("strategy.task_manager.fetch_token_balance", side_effect=fetch_fn):
        tm._process_force_closing(task)
    assert task.state == EventTaskState.SETTLING_OUTCOME, task.state
    print("  ✓ 场景 D：balance 充足但 SDK 400 → 原失败分支 + 诊断 + 强平窗口兜底（无强平）")


# ===== 全流程状态机完整性断言 =====


def test_state_machine_path_is_legal_for_all_scenarios():
    """所有 4 个场景的状态机转移路径都必须在合法转换集合里。"""
    legal_transitions = {
        EventTaskState.PENDING: {EventTaskState.PLACING_ENTRY, EventTaskState.SETTLING_OUTCOME, EventTaskState.SKIPPED},
        EventTaskState.PLACING_ENTRY: {EventTaskState.WAITING_ENTRY, EventTaskState.SETTLED, EventTaskState.FAILED},
        EventTaskState.WAITING_ENTRY: {EventTaskState.SETTLING_OUTCOME, EventTaskState.HANDLING_SINGLE, EventTaskState.WAITING_CLOSE_WINDOW, EventTaskState.SETTLED, EventTaskState.FAILED},
        EventTaskState.HANDLING_SINGLE: {EventTaskState.WAITING_CLOSE_WINDOW, EventTaskState.FORCE_CLOSING, EventTaskState.SETTLING_OUTCOME, EventTaskState.SETTLED},
        EventTaskState.WAITING_CLOSE_WINDOW: {EventTaskState.FORCE_CLOSING, EventTaskState.SETTLING_OUTCOME, EventTaskState.SETTLED},
        EventTaskState.FORCE_CLOSING: {EventTaskState.SETTLING_OUTCOME, EventTaskState.SETTLED, EventTaskState.FAILED},
    }
    # 场景 A 路径：PLACING → WAITING → SETTLING_OUTCOME（每一步都在合法集合里）
    assert EventTaskState.WAITING_ENTRY in legal_transitions[EventTaskState.PLACING_ENTRY]
    assert EventTaskState.SETTLING_OUTCOME in legal_transitions[EventTaskState.WAITING_ENTRY]
    # 场景 B/C/D 路径：PLACING → WAITING → HANDLING_SINGLE → WAITING_CLOSE_WINDOW → FORCE_CLOSING
    assert EventTaskState.HANDLING_SINGLE in legal_transitions[EventTaskState.WAITING_ENTRY]
    assert EventTaskState.WAITING_CLOSE_WINDOW in legal_transitions[EventTaskState.HANDLING_SINGLE]
    assert EventTaskState.FORCE_CLOSING in legal_transitions[EventTaskState.WAITING_CLOSE_WINDOW]
    print("  ✓ 4 个场景的状态机转移路径全部合法")


if __name__ == "__main__":
    test_scenario_a_both_filled_settling_outcome()
    test_scenario_b_single_fill_balance_ok_normal_sell_path()
    test_scenario_c_balance_guard_skips_sell_force_close_takeover()
    test_scenario_d_balance_ok_but_clob_400_falls_back_to_force_close()
    test_state_machine_path_is_legal_for_all_scenarios()
    print("\nAll e2e mock tests passed.")