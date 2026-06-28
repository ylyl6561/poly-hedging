"""
诊断：异步刷新后 status=filled 但 filled_shares=None 的影响路径。
通过 mock 直接复用真实代码路径，验证 fallback 逻辑是否把理论 shares 误当成 fill。
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


def main():
    """模拟：CLOB 返回 status=filled，filled_shares=None"""
    from strategy.dual_wallet_executor import ExecutionOutcome

    # 模拟场景1：SDK 在 raw 里填 status=filled，但 filled_shares 字段为 None
    # （这是 py_clob_client_v2 实际行为：GET /orders/{id} 的响应字段不直接是 filled_shares）
    outcome_a = ExecutionOutcome(
        success=True,
        order_id="0xabc",
        price=0.59,
        shares=None,           # ← SDK 没填
        filled_shares=None,    # ← SDK 没填
        filled_amount_usd=None,
        raw={"status": "matched", "size_matched": "5000000"},
    )

    # 模拟场景2：SDK 填了 filled_shares=0（限价单挂在 best_ask 没成交）
    outcome_b = ExecutionOutcome(
        success=True,
        order_id="0xdef",
        price=0.59,
        shares=None,
        filled_shares=0,         # ← 关键：0 不是 None
        filled_amount_usd=0,
        raw={"status": "live"},
    )

    # 模拟 task 的 snapshot 状态（entry 阶段 mark_order 后的样子）
    from strategy.dual_wallet_models import OrderSide, OperationType, OrderStatus, OrderSnapshot
    from dataclasses import dataclass
    from datetime import datetime, timezone

    @dataclass
    class FakeWallet:
        wallet_id: str = "wallet_b"
        wallet_name: str = "B"
        role: Any = None

    # entry 后 snapshot 状态：status=SUBMITTED，shares=5.0（理论值，task_manager.py:725-726 写死）
    snap_after_entry = OrderSnapshot(
        wallet=FakeWallet(),
        event_name="test",
        side=OrderSide.DOWN,
        amount_usd=2.95,
        operation=OperationType.PLACE,
        order_id="0xabc",
        status=OrderStatus.SUBMITTED.value,
        shares=5.0,            # ← task_manager 强制写入 entry_shares
        filled_shares=None,    # ← entry 时还没 fill
        filled_amount_usd=None,
    )

    print("=" * 70)
    print("场景A：异步刷新 outcome.filled_shares=None")
    print("=" * 70)
    snapshot_a = snap_after_entry
    prev_status = snapshot_a.status
    raw_status = str((outcome_a.raw or {}).get("status") or "").lower()
    print(f"  prev_status = {prev_status!r}")
    print(f"  CLOB raw status = {raw_status!r}")

    # 模拟 _refresh_order_statuses 里的 status 归一化
    normalized = {
        "live": "submitted", "open": "submitted", "pending": "submitted",
        "matched": "filled", "filled": "filled", "executed": "filled",
        "cancelled": "cancelled", "canceled": "cancelled",
        "failed": "failed", "rejected": "failed",
    }.get(raw_status, raw_status)
    print(f"  normalized_status = {normalized!r}")
    snapshot_a.status = normalized

    if outcome_a.shares is not None:
        snapshot_a.shares = float(outcome_a.shares)
    if outcome_a.filled_shares is not None:
        snapshot_a.filled_shares = float(outcome_a.filled_shares)

    print(f"  refresh 后: status={snapshot_a.status!r}, filled_shares={snapshot_a.filled_shares}")
    print(f"  snapshot.shares (理论值) = {snapshot_a.shares}")

    # 现在调 check_single_side_filled 的核心逻辑
    is_filled = snapshot_a.status == "filled"
    live_filled_shares = float(snapshot_a.filled_shares or snapshot_a.shares or 0.0)
    print(f"  >> check_single_side_filled: status=='filled'? {is_filled}")
    print(f"  >> live_filled_shares = {snapshot_a.filled_shares} or {snapshot_a.shares} = {live_filled_shares}")
    print(f"  >> ⚠️ TASK 认为钱包 B 成交了 {live_filled_shares} shares（理论值）")

    print()
    print("=" * 70)
    print("场景B：异步刷新 outcome.filled_shares=0")
    print("=" * 70)
    snapshot_b = OrderSnapshot(
        wallet=FakeWallet(),
        event_name="test",
        side=OrderSide.DOWN,
        amount_usd=2.95,
        operation=OperationType.PLACE,
        order_id="0xdef",
        status=OrderStatus.SUBMITTED.value,
        shares=5.0,
        filled_shares=None,
    )
    if outcome_b.shares is not None:
        snapshot_b.shares = float(outcome_b.shares)
    if outcome_b.filled_shares is not None:    # 0 is not None → True
        snapshot_b.filled_shares = float(outcome_b.filled_shares)
    print(f"  refresh 后: status={snapshot_b.status!r}, filled_shares={snapshot_b.filled_shares}")
    # CLOB raw status="live" → normalized="submitted" → 不会进单边

    print()
    print("=" * 70)
    print("【结论】")
    print("=" * 70)
    print("当 CLOB 返回 status='matched/filled' 但 SDK 没填 filled_shares 字段时：")
    print("  - snapshot.status 被改成 'filled'")
    print("  - snapshot.filled_shares 仍是 None")
    print("  - 下游 fallback: filled_shares or shares → 用理论值 5.00")
    print("  - task 误判 '已成交 5 份' → 挂 GTC 抛售 5 份")
    print("  - CLOB 实际余额=0（根本没收到 fill），抛售单被拒")
    print()
    print("根本原因：")
    print("  1. entry 阶段 task_manager.py:725-726 把 snapshot.shares 写死成 entry_shares")
    print("  2. refresh 阶段 task_manager.py:1424 只在 outcome.filled_shares is not None 时覆盖")
    print("  3. SDK 在 BUY 路径下永远返回 shares=amount/price（理论值），不是 fill 份额")
    print("  4. _normalize_result dual_wallet_executor.py:218 用 or 链 → 0 被吞")
    print()
    print("=== 关键路径：async 改动前 vs 后 ===")
    print("async 改动前：串行 refresh，每个 wallet 一笔，逻辑等价")
    print("async 改动后：并发 refresh + 节流 0.25s + ThreadPool + 节流早返回")
    print("  → 但代码逻辑跟以前是等价的，问题不在并发本身")
    print("  → 真正问题是：BUY 路径下 filled_shares 字段一直没被正确填过")


if __name__ == "__main__":
    main()