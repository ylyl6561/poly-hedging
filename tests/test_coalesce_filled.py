"""单元测试：coalesce_filled_shares(filled, target) helper。

覆盖核心场景：
1. filled=None / target=None → 0
2. filled=None / target=5.0 → 5.0（fallback）
3. filled=0 / target=5.0 → 0（关键：不能 fallback！）
4. filled=0 / target=None → 0
5. filled=5.0 / target=10.0 → 5.0（filled 优先）
6. filled=5.0 / target=None → 5.0
7. 边界：None / 字符串 / 负数
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

if "eth_account" not in sys.modules:
    _fake = types.ModuleType("eth_account")
    _fake.Account = MagicMock()
    sys.modules["eth_account"] = _fake

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.event_task import coalesce_filled_shares


def test_both_none():
    assert coalesce_filled_shares(None, None) == 0.0
    print("  ✓ filled=None, target=None → 0.0")


def test_filled_none_fallback_to_target():
    assert coalesce_filled_shares(None, 5.0) == 5.0
    print("  ✓ filled=None, target=5.0 → 5.0 (fallback)")


def test_filled_zero_does_not_fallback():
    """关键 bug 修复点：原来 filled_shares or target_shares → 0 or 5.0 = 5.0"""
    assert coalesce_filled_shares(0, 5.0) == 0.0, "0 must NOT fallback to target!"
    print("  ✓ filled=0, target=5.0 → 0.0 (NOT 5.0, bug fix)")


def test_filled_zero_target_none():
    assert coalesce_filled_shares(0, None) == 0.0
    print("  ✓ filled=0, target=None → 0.0")


def test_filled_takes_priority():
    assert coalesce_filled_shares(5.0, 10.0) == 5.0
    print("  ✓ filled=5.0, target=10.0 → 5.0 (filled wins)")


def test_filled_value_target_none():
    assert coalesce_filled_shares(5.0, None) == 5.0
    print("  ✓ filled=5.0, target=None → 5.0")


def test_edge_string_inputs():
    """防止 SDK 返回字符串数字。"""
    assert coalesce_filled_shares("3.5", "10.0") == 3.5
    assert coalesce_filled_shares(None, "5.0") == 5.0
    print("  ✓ string 数字自动转 float")


def test_edge_invalid_inputs():
    assert coalesce_filled_shares("not_a_number", 5.0) == 5.0  # filled 解析失败 → fallback
    assert coalesce_filled_shares(None, "garbage") == 0.0  # target 解析失败 → 0
    print("  ✓ 解析失败时降级到 0 或 fallback")


def test_zero_string_treated_as_zero():
    """'0' 是 truthy string，不影响：coerce 成 0。"""
    assert coalesce_filled_shares("0", 5.0) == 0.0, "string '0' should coerce to 0.0, not fallback"
    print("  ✓ '0' 解析成 0.0，不被 fallback")


def test_negative():
    """负数（理论上不该出现，但不应 crash）。"""
    assert coalesce_filled_shares(-1.0, 5.0) == -1.0  # 透传
    print("  ✓ 负数透传")


if __name__ == "__main__":
    test_both_none()
    test_filled_none_fallback_to_target()
    test_filled_zero_does_not_fallback()
    test_filled_zero_target_none()
    test_filled_takes_priority()
    test_filled_value_target_none()
    test_edge_string_inputs()
    test_edge_invalid_inputs()
    test_zero_string_treated_as_zero()
    test_negative()
    print("ALL COALESCE TESTS PASSED")