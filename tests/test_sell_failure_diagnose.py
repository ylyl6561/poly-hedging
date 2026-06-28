"""
诊断函数单元测试（不依赖完整 runtime，仅测试：
1. _parse_clob_balance_error 解析 CLOB 错误文本
2. _diagnose_sell_balance_mismatch 输出关键字段
"""
from __future__ import annotations

import io
import sys
import types
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

if "eth_account" not in sys.modules:
    _fake = types.ModuleType("eth_account")
    _fake.Account = MagicMock()
    sys.modules["eth_account"] = _fake

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import strategy.task_manager as tm_module


def test_parse_balance_error_basic():
    """标准 'balance: 0, order amount: 5000000'。"""
    err = (
        "PolyApiException[status_code=400, error_message={'error': "
        "'not enough balance / allowance: the balance is not enough -> "
        "balance: 0, order amount: 5000000'}]"
    )
    parsed = tm_module.TaskManager._parse_clob_balance_error(err)
    assert parsed["status_code"] == 400, parsed
    assert parsed["balance_raw"] == 0.0, parsed
    assert parsed["order_amount_raw"] == 5000000.0, parsed
    assert parsed["balance_shares"] == 0.0, parsed
    assert parsed["order_amount_shares"] == 5.0, parsed
    print("  ✓ 解析 balance=0 / order_amount=5_000_000 (5 shares)")


def test_parse_balance_error_non_zero():
    """余额非零的情况。"""
    err = "balance: 3500000, order amount: 5000000"
    parsed = tm_module.TaskManager._parse_clob_balance_error(err)
    assert parsed["balance_shares"] == 3.5
    assert parsed["order_amount_shares"] == 5.0
    print("  ✓ 解析 balance=3.5 / order_amount=5.0 shares")


def test_parse_balance_error_empty():
    """空 / 非余额错误：返回空 dict。"""
    assert tm_module.TaskManager._parse_clob_balance_error("") == {}
    assert tm_module.TaskManager._parse_clob_balance_error(None) == {}
    assert tm_module.TaskManager._parse_clob_balance_error("some random error") == {}
    print("  ✓ 非余额错误返回空 dict")


# ===== 诊断输出测试 =====


from strategy.dual_wallet_models import OrderSide as RealOrderSide, OperationType as RealOpType


@dataclass
class FakeWallet:
    wallet_id: str = "wallet_b"
    wallet_name: str = "B"


@dataclass
class FakeSnapshot:
    operation: Any = None
    side: Any = None
    price: float | None = 0.59
    shares: float | None = 5.0
    filled_shares: float | None = 5.0
    status: str | None = "filled"
    token_id: str | None = None
    order_id: str | None = "0xabc"
    filled_amount_usd: float | None = 2.95


def _make_history_snapshot():
    return FakeSnapshot(operation=RealOpType.PLACE, side=RealOrderSide.DOWN, status="filled", order_id="0xabc")


@dataclass
class FakeTask:
    clob_token_ids: list[str] = field(default_factory=lambda: ["12345_up_token", "67890_down_token"])
    condition_id: str = "0xcond"

    def get_order_history(self, wallet_id):
        return [_make_history_snapshot()]


def test_diagnose_balance_zero_outputs_root_cause_hint():
    """balance=0 时诊断日志必须出现 'DIAGNOSIS: CLOB reports wallet balance=0' 关键字。"""
    tm = tm_module.TaskManager.__new__(tm_module.TaskManager)  # skip __init__
    buf = io.StringIO()
    err = (
        "PolyApiException[status_code=400, error_message={'error': "
        "'not enough balance / allowance: the balance is not enough -> "
        "balance: 0, order amount: 5000000'}]"
    )
    with contextlib.redirect_stdout(buf):
        tm._diagnose_sell_balance_mismatch(
            task=FakeTask(),
            wallet=FakeWallet(),
            side=RealOrderSide.DOWN,
            shares_attempted=5.0,
            price_attempted=0.59,
            error_text=err,
        )
    out = buf.getvalue()
    assert "balance=0" in out, out
    assert "CLOB raw error" in out, out
    assert "balance_raw=0" in out, out
    assert "order_amount_raw=5000000" in out, out
    assert "task.clob_token_ids[1] = 67890_down_token" in out, out
    assert "shares_attempted = 5.0000" in out, out
    assert "raw_shares_attempted (CLOB decimals) = 5000000" in out, out
    assert "Possible causes" in out, out
    assert "0xabc" in out, out
    print("  ✓ balance=0 诊断输出完整（含 root cause hint + 订单历史）")


def test_diagnose_partial_fill_drift():
    """balance < shares_attempted 时提示余额漂移。"""
    tm = tm_module.TaskManager.__new__(tm_module.TaskManager)
    buf = io.StringIO()
    err = "balance: 2500000, order amount: 5000000"  # 2.5 vs 5.0
    with contextlib.redirect_stdout(buf):
        tm._diagnose_sell_balance_mismatch(
            task=FakeTask(),
            wallet=FakeWallet(),
            side=RealOrderSide.DOWN,
            shares_attempted=5.0,
            price_attempted=0.59,
            error_text=err,
        )
    out = buf.getvalue()
    assert "balance (2.5000) < sell attempt (5.0000)" in out, out
    assert "漂移" in out, out
    print("  ✓ 部分 fill 漂移诊断触发")


def test_diagnose_no_local_history_outputs_warning():
    """本地订单历史为空时诊断必须告警。"""
    @dataclass
    class EmptyTask:
        clob_token_ids: list[str] = field(default_factory=lambda: ["u", "d"])
        def get_order_history(self, wallet_id):
            return []

    tm = tm_module.TaskManager.__new__(tm_module.TaskManager)
    buf = io.StringIO()
    err = "balance: 0, order amount: 5000000"
    with contextlib.redirect_stdout(buf):
        tm._diagnose_sell_balance_mismatch(
            task=EmptyTask(),
            wallet=FakeWallet(),
            side=RealOrderSide.DOWN,
            shares_attempted=5.0,
            price_attempted=0.59,
            error_text=err,
        )
    out = buf.getvalue()
    assert "本地订单历史为空" in out, out
    print("  ✓ 本地历史为空时告警")


if __name__ == "__main__":
    test_parse_balance_error_basic()
    test_parse_balance_error_non_zero()
    test_parse_balance_error_empty()
    test_diagnose_balance_zero_outputs_root_cause_hint()
    test_diagnose_partial_fill_drift()
    test_diagnose_no_local_history_outputs_warning()
    print("ALL DIAGNOSE TESTS PASSED")