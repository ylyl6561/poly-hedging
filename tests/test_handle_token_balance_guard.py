"""
【P0/P1 修复相关测试】

覆盖：
1. ``api.fetch_token_balance`` 的响应解析（_extract_token_balance）
2. ``_process_handling_single`` 在 on-chain token balance 不够时跳过 sell，
   不调用 place_gtc_sell_order、不抛 400，而是写一条 FAILED 快照并进
   WAITING_CLOSE_WINDOW（状态机转移与原版本一致）。
3. 当 balance 充足时仍正常挂 sell（防止回归）。
4. balance 接口本身失败时不阻塞挂 sell（fail-open，保留原行为）。
"""
from __future__ import annotations

import contextlib
import io
import sys
import types
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

if "eth_account" not in sys.modules:
    _fake = types.ModuleType("eth_account")
    _fake.Account = MagicMock()
    sys.modules["eth_account"] = _fake

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api.api as api_module
import strategy.task_manager as tm_module
from api.api import fetch_token_balance, _extract_token_balance
from strategy.event_task_state import EventTaskState
from strategy.dual_wallet_models import (
    OperationType,
    OrderSide,
    OrderSnapshot,
    OrderStatus,
)


# ===== _extract_token_balance 解析测试 =====


def test_extract_token_balance_dict_balance_int():
    """标准 dict 形如 {'balance': '5000000'} → 5.0 shares。"""
    assert _extract_token_balance({"balance": "5000000"}) == 5.0
    print("  ✓ dict[balance=str] → 5.0 shares")


def test_extract_token_balance_dict_balance_float():
    assert _extract_token_balance({"balance": 2500000}) == 2.5
    print("  ✓ dict[balance=int] → 2.5 shares")


def test_extract_token_balance_nested_data():
    """嵌套在 data.balance 下也能解析。"""
    assert _extract_token_balance({"data": {"balance": "5000000"}}) == 5.0
    print("  ✓ dict[data.balance] → 5.0 shares")


def test_extract_token_balance_list_payload():
    """List 形如 [{'balance': ...}, ...] 取首个有效。"""
    assert _extract_token_balance([{"balance": "1000000"}, {"balance": "2000000"}]) == 1.0
    print("  ✓ list[dict] → 1.0 shares")


def test_extract_token_balance_returns_none_when_missing():
    """无 balance 字段 → None。"""
    assert _extract_token_balance({"foo": "bar"}) is None
    assert _extract_token_balance(None) is None
    assert _extract_token_balance("not a dict") is None
    print("  ✓ 无 balance 字段返回 None")


# ===== fetch_token_balance 接口测试 =====


@dataclass
class FakeAccount:
    account_id: str = "acct_a"
    label: str = "Account A"
    signature_type: int = 2
    wallet_address: str = "0xw"


def test_fetch_token_balance_success_int():
    """成功路径：SDK 返回 dict with balance=int → shares 换算正确。"""
    fake_client = MagicMock()
    fake_client.get_balance_allowance = MagicMock(return_value={"balance": "5000000"})
    with patch.object(api_module, "get_direct_clob_client", return_value=fake_client):
        result = fetch_token_balance(asset_id="token_up", account=FakeAccount())
    assert result["success"] is True
    assert result["balance_shares"] == 5.0
    assert result["asset_id"] == "token_up"
    print("  ✓ fetch_token_balance 成功，balance_shares=5.0")


def test_fetch_token_balance_mock():
    """mock=True 直接返回 0，避免真实网络调用。"""
    result = fetch_token_balance(asset_id="token_up", account=FakeAccount(), mock=True)
    assert result["success"] is True
    assert result["balance_shares"] == 0.0
    assert result.get("raw", {}).get("mock") is True
    print("  ✓ mock=True 返回 balance=0")


def test_fetch_token_balance_missing_asset_id():
    result = fetch_token_balance(asset_id="", account=FakeAccount())
    assert result["success"] is False
    assert result["balance_shares"] is None
    assert "missing_asset_id" in result["error"]
    print("  ✓ 缺 asset_id 返回 success=False")


def test_fetch_token_balance_sdk_returns_no_balance_fields():
    """SDK 返回 dict 但无 balance 字段：success=False，balance_shares=None。"""
    fake_client = MagicMock()
    fake_client.get_balance_allowance = MagicMock(return_value={"unrelated": "value"})
    with patch.object(api_module, "get_direct_clob_client", return_value=fake_client):
        result = fetch_token_balance(asset_id="token_up", account=FakeAccount())
    assert result["success"] is False
    assert result["balance_shares"] is None
    print("  ✓ SDK 返回无 balance 字段时 success=False")


# ===== _process_handling_single balance guard 集成测试 =====


def make_real_task() -> EventTask:
    """构造真实 EventTask，UP entry 已 fill 5.0 股（status=FILLED），DOWN entry 未 fill（status=SUBMITTED）。"""
    from strategy.event_task import EventTask as RealEventTask
    from datetime import datetime, timedelta, timezone
    from strategy.dual_wallet_models import WalletIdentity, WalletRole

    up_wallet = WalletIdentity(
        wallet_id="wallet_a",
        wallet_name="A",
        account=FakeAccount(account_id="acct_a"),
        role=WalletRole.A,
    )
    down_wallet = WalletIdentity(
        wallet_id="wallet_b",
        wallet_name="B",
        account=FakeAccount(account_id="acct_b"),
        role=WalletRole.B,
    )
    now = datetime.now(timezone.utc)
    task = RealEventTask(
        event_name="test_event",
        event_id="evt_test",
        condition_id="0xcond",
        clob_token_ids=["token_up_id", "token_down_id"],
        start_time=now - timedelta(minutes=1),
        end_time=now + timedelta(seconds=30),
        close_window_sec=20,
        wallets=[up_wallet, down_wallet],
        side_by_wallet_id={up_wallet.wallet_id: OrderSide.UP, down_wallet.wallet_id: OrderSide.DOWN},
        first_fill_wallet_id=up_wallet.wallet_id,
        metadata={"fee_rate_bps": 0},
    )
    # 手动注入 entry 订单快照（UP=FILLED, DOWN=SUBMITTED），模拟 entry 阶段已完成
    up_entry = OrderSnapshot(
        wallet=up_wallet,
        event_name="test_event",
        side=OrderSide.UP,
        amount_usd=2.5,
        operation=OperationType.PLACE,
        order_id="0xentry_up",
        token_id="token_up_id",
        condition_id="0xcond",
        price=0.5,
        shares=5.0,
        status=OrderStatus.FILLED.value,
        filled_shares=5.0,
        filled_amount_usd=2.5,
    )
    down_entry = OrderSnapshot(
        wallet=down_wallet,
        event_name="test_event",
        side=OrderSide.DOWN,
        amount_usd=2.5,
        operation=OperationType.PLACE,
        order_id="0xentry_down",
        token_id="token_down_id",
        condition_id="0xcond",
        price=0.5,
        shares=5.0,
        status=OrderStatus.SUBMITTED.value,
        filled_shares=0.0,
        filled_amount_usd=0.0,
    )
    task.mark_order(up_entry)
    task.mark_order(down_entry)
    return task


def _make_manager():
    return tm_module.TaskManager.__new__(tm_module.TaskManager)


def test_handling_single_skips_sell_when_balance_insufficient():
    """balance=0 时：place_gtc_sell_order 不被调用，写一条 FAILED 快照，状态 → WAITING_CLOSE_WINDOW。"""
    tm = _make_manager()
    tm.config = MagicMock()
    tm.config.fixed_sell_price = 0.76
    tm.config.dry_run = False

    task = make_real_task()
    # 强制把状态推到 HANDLING_SINGLE（绕过 transition_to 的合法性检查）
    task.state = EventTaskState.HANDLING_SINGLE
    tm._order_exec = MagicMock()

    balance_resp = {
        "success": True,
        "balance_shares": 0.0,
        "asset_id": "token_up_id",
        "raw": {"balance": "0"},
    }
    with patch("strategy.task_manager.fetch_token_balance", return_value=balance_resp):
        with contextlib.redirect_stdout(io.StringIO()):
            tm._process_handling_single(task)

    # 关键约束：place_gtc_sell_order 必须不被调用（避免 CLOB 400）
    tm._order_exec.place_gtc_sell_order.assert_not_called()
    # 关键：必须写到一条 FAILED 快照（仅 history，不覆盖 current_order）
    failed_sell_snaps = [
        s for s in task.get_order_history("wallet_a")
        if s.operation == OperationType.SELL and s.status == OrderStatus.FAILED.value
    ]
    assert failed_sell_snaps, f"未写入 FAILED 快照: {task.get_order_history('wallet_a')}"
    assert "on_chain_balance_mismatch" in (failed_sell_snaps[-1].error or "")
    # 关键：balance guard 不应覆盖 live_wallet 的 current_order（保持原 entry PLACE 单可见，
    # 让 _execute_force_close 能正常撤 entry + FAK 强平 UP 仓位）
    current = task.get_order("wallet_a")
    assert current is not None
    assert current.operation == OperationType.PLACE, (
        f"current_order 被覆盖为 {current.operation}，应该保留为 PLACE 让强平窗口能 FAK"
    )
    # 状态机必须推进到 WAITING_CLOSE_WINDOW（与原版本契约一致）
    assert task.state == EventTaskState.WAITING_CLOSE_WINDOW, task.state
    print("  ✓ balance=0 → 跳过 sell、写 FAILED 快照（仅 history）、状态推进、current_order 不被覆盖")


def test_handling_single_proceeds_when_balance_sufficient():
    """balance >= sell_shares 时：正常调用 place_gtc_sell_order。"""
    tm = _make_manager()
    tm.config = MagicMock()
    tm.config.fixed_sell_price = 0.76
    tm.config.dry_run = False

    task = make_real_task()
    task.state = EventTaskState.HANDLING_SINGLE
    tm._order_exec = MagicMock()
    sell_op_result = MagicMock()
    sell_op_result.outcome.success = True
    sell_op_result.outcome.order_id = "0xsell"
    sell_op_result.snapshot = OrderSnapshot(
        wallet=task.get_up_wallet(),
        event_name="test_event",
        side=OrderSide.UP,
        amount_usd=5.0 * 0.76,
        operation=OperationType.SELL,
        order_id="0xsell",
        price=0.76,
        shares=5.0,
        status=OrderStatus.SUBMITTED.value,
    )
    tm._order_exec.place_gtc_sell_order.return_value = sell_op_result

    balance_resp = {
        "success": True,
        "balance_shares": 5.0,
        "asset_id": "token_up_id",
        "raw": {"balance": "5000000"},
    }
    with patch("strategy.task_manager.fetch_token_balance", return_value=balance_resp):
        with contextlib.redirect_stdout(io.StringIO()):
            tm._process_handling_single(task)

    tm._order_exec.place_gtc_sell_order.assert_called_once()
    print("  ✓ balance=5.0 → 正常挂 sell（无回归）")


def test_handling_single_balance_check_fail_open_when_sdk_errors():
    """balance 接口失败：fail-open，仍允许挂 sell（保留旧行为，不阻塞）。"""
    tm = _make_manager()
    tm.config = MagicMock()
    tm.config.fixed_sell_price = 0.76
    tm.config.dry_run = False

    task = make_real_task()
    task.state = EventTaskState.HANDLING_SINGLE
    tm._order_exec = MagicMock()
    sell_op_result = MagicMock()
    sell_op_result.outcome.success = True
    sell_op_result.outcome.order_id = "0xsell"
    sell_op_result.snapshot = OrderSnapshot(
        wallet=task.get_up_wallet(),
        event_name="test_event",
        side=OrderSide.UP,
        amount_usd=5.0 * 0.76,
        operation=OperationType.SELL,
        order_id="0xsell",
        price=0.76,
        shares=5.0,
        status=OrderStatus.SUBMITTED.value,
    )
    tm._order_exec.place_gtc_sell_order.return_value = sell_op_result

    balance_resp = {"success": False, "balance_shares": None, "error": "sdk_unavailable"}
    with patch("strategy.task_manager.fetch_token_balance", return_value=balance_resp):
        with contextlib.redirect_stdout(io.StringIO()):
            tm._process_handling_single(task)

    # balance 检查失败时仍走原路径，不阻塞业务
    tm._order_exec.place_gtc_sell_order.assert_called_once()
    print("  ✓ balance 接口失败时 fail-open（不阻塞挂 sell）")


def test_handling_single_balance_partial_skips():
    """balance=2.0，期望 5.0：触发跳过（防止 CLOB 400）。"""
    tm = _make_manager()
    tm.config = MagicMock()
    tm.config.fixed_sell_price = 0.76
    tm.config.dry_run = False

    task = make_real_task()
    task.state = EventTaskState.HANDLING_SINGLE
    tm._order_exec = MagicMock()

    balance_resp = {
        "success": True,
        "balance_shares": 2.0,
        "asset_id": "token_up_id",
        "raw": {"balance": "2000000"},
    }
    with patch("strategy.task_manager.fetch_token_balance", return_value=balance_resp):
        with contextlib.redirect_stdout(io.StringIO()):
            tm._process_handling_single(task)

    tm._order_exec.place_gtc_sell_order.assert_not_called()
    print("  ✓ balance 部分到账 → 跳过 sell")


if __name__ == "__main__":
    test_extract_token_balance_dict_balance_int()
    test_extract_token_balance_dict_balance_float()
    test_extract_token_balance_nested_data()
    test_extract_token_balance_list_payload()
    test_extract_token_balance_returns_none_when_missing()

    test_fetch_token_balance_success_int()
    test_fetch_token_balance_mock()
    test_fetch_token_balance_missing_asset_id()
    test_fetch_token_balance_sdk_returns_no_balance_fields()

    test_handling_single_skips_sell_when_balance_insufficient()
    test_handling_single_proceeds_when_balance_sufficient()
    test_handling_single_balance_check_fail_open_when_sdk_errors()
    test_handling_single_balance_partial_skips()

    print("\nAll tests passed.")