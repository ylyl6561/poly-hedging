"""
Mock 订单执行器 - 用于测试和模拟交易流程

不真实下单，但模拟整个流程的行为：
1. 挂单立即成功（返回虚拟 order_id）
2. 订单状态可配置（用于模拟不同成交场景）
3. 支持时间触发的成交模拟
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from strategy.dual_wallet_models import (
    OrderSide,
    OrderSnapshot,
    OrderStatus,
    OperationType,
    WalletIdentity,
)
from strategy.order_executor_v2 import (
    ExecutionOutcome,
    OrderExecutorProtocol,
)


@dataclass
class MockOrderConfig:
    """Mock 订单配置"""
    fill_side: str = "UP"  # UP, DOWN, BOTH, NONE
    fill_after_sec: float = 5.0  # 多少秒后成交
    fill_immediately: bool = False  # 是否立即成交


class MockOrderRegistry:
    """
    Mock 订单注册表 - 管理所有 Mock 订单的状态

    每个订单有：
    - order_id: 虚拟订单 ID
    - status: 订单状态 (pending, submitted, filled, cancelled, failed)
    - fill_time: 预计成交时间
    - actual_fill_time: 实际成交时间
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, '_initialized', False):
            return
        self._orders: dict[str, dict] = {}
        self._counter = 0
        self._order_lock = threading.Lock()
        self._initialized = True

    def reset(self):
        """重置所有订单状态"""
        with self._order_lock:
            self._orders.clear()
            self._counter = 0

    def create_order(
        self,
        wallet_id: str,
        side: str,
        operation: str,  # place, sell, force_close
        fill_after_sec: float | None = None,
        fill_side: str = "UP",
    ) -> str:
        """创建一个新订单"""
        with self._order_lock:
            self._counter += 1
            order_id = f"MOCK_{self._counter}_{int(time.time() * 1000) % 100000}"

            fill_time = None
            if fill_after_sec is not None and fill_after_sec > 0:
                fill_time = datetime.now(timezone.utc).timestamp() + fill_after_sec

            self._orders[order_id] = {
                "order_id": order_id,
                "wallet_id": wallet_id,
                "side": side,
                "operation": operation,
                "status": OrderStatus.SUBMITTED.value,
                "fill_time": fill_time,
                "fill_side": fill_side,
                "filled_shares": 0.0,
                "filled_amount_usd": 0.0,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            return order_id

    def get_order(self, order_id: str) -> dict | None:
        """获取订单状态（自动检查是否应该成交）"""
        with self._order_lock:
            if order_id not in self._orders:
                return None

            order = self._orders[order_id].copy()

            # 自动成交检查
            if order["status"] == OrderStatus.SUBMITTED.value and order["fill_time"]:
                now = datetime.now(timezone.utc).timestamp()
                if now >= order["fill_time"]:
                    order["status"] = OrderStatus.FILLED.value
                    order["filled_shares"] = 1.0  # 模拟成交份额
                    order["filled_amount_usd"] = 0.5  # 模拟成交金额
                    self._orders[order_id]["status"] = OrderStatus.FILLED.value
                    self._orders[order_id]["filled_shares"] = 1.0
                    self._orders[order_id]["filled_amount_usd"] = 0.5

            return order

    def update_order_status(self, order_id: str, status: str) -> bool:
        """更新订单状态"""
        with self._order_lock:
            if order_id in self._orders:
                self._orders[order_id]["status"] = status
                return True
            return False

    def cancel_order(self, order_id: str) -> bool:
        """取消订单"""
        return self.update_order_status(order_id, OrderStatus.CANCELLED.value)

    def is_filled(self, order_id: str) -> bool:
        """检查订单是否已成交"""
        order = self.get_order(order_id)
        return order is not None and order["status"] == OrderStatus.FILLED.value

    def get_filled_shares(self, order_id: str) -> float:
        """获取成交份额"""
        order = self.get_order(order_id)
        if order:
            return float(order.get("filled_shares", 0.0))
        return 0.0

    def get_all_orders(self) -> list[dict]:
        """获取所有订单"""
        with self._order_lock:
            return list(self._orders.values())


class MockOrderExecutor:
    """
    Mock 订单执行器

    实现 OrderExecutorProtocol，但所有操作都是模拟的。
    用于测试和开发，不需要真实的 Polymarket 账户。
    """

    def __init__(self, config: MockOrderConfig | None = None):
        self.config = config or MockOrderConfig()
        self.registry = MockOrderRegistry()

    def place_entry(
        self,
        wallet,
        event_name: str,
        side: OrderSide,
        amount_usd: float,
        price: float,
        clob_token_ids: list[str],
        fee_rate_bps: int,
        condition_id: str,
        order_type_override: str | None = None,
        post_only: bool = False,
    ) -> ExecutionOutcome:
        """挂初始买单（Mock）"""
        wallet_id = getattr(wallet, 'wallet_id', str(wallet))
        side_str = side.value if hasattr(side, 'value') else str(side)

        # 计算成交时机
        fill_after_sec = self.config.fill_after_sec if not self.config.fill_immediately else 0

        order_id = self.registry.create_order(
            wallet_id=wallet_id,
            side=side_str,
            operation="place",
            fill_after_sec=fill_after_sec,
            fill_side=self.config.fill_side,
        )

        print(f"   [MOCK] place_entry: {wallet_id} {side_str} @ {price} (fill_after={fill_after_sec}s)")

        return ExecutionOutcome(
            success=True,
            order_id=order_id,
            price=price,
            shares=amount_usd / price,
            filled_shares=0.0,
            filled_amount_usd=0.0,
            note=f"mock_entry_{side_str}",
        )

    def place_sell(
        self,
        wallet,
        event_name: str,
        side: OrderSide,
        shares: float,
        price: float,
        clob_token_ids: list[str],
        fee_rate_bps: int,
        condition_id: str,
        order_type_override: str | None = None,
        post_only: bool = False,
    ) -> ExecutionOutcome:
        """挂抛售单（Mock）"""
        wallet_id = getattr(wallet, 'wallet_id', str(wallet))
        side_str = side.value if hasattr(side, 'value') else str(side)
        order_type = order_type_override or "GTC"

        # 计算成交时机
        fill_after_sec = self.config.fill_after_sec if not self.config.fill_immediately else 0

        order_id = self.registry.create_order(
            wallet_id=wallet_id,
            side=side_str,
            operation="sell" if order_type == "GTC" else "force_close",
            fill_after_sec=fill_after_sec,
            fill_side=self.config.fill_side,
        )

        print(f"   [MOCK] place_sell: {wallet_id} {side_str} @ {price} (fill_after={fill_after_sec}s, type={order_type})")

        return ExecutionOutcome(
            success=True,
            order_id=order_id,
            price=price,
            shares=shares,
            filled_shares=0.0,
            filled_amount_usd=0.0,
            note=f"mock_sell_{side_str}_{order_type}",
        )

    def cancel(self, order_id: str | None, wallet) -> ExecutionOutcome:
        """撤单（Mock）"""
        if not order_id:
            return ExecutionOutcome(
                success=False,
                error="No order_id provided",
            )

        wallet_id = getattr(wallet, 'wallet_id', str(wallet))
        success = self.registry.cancel_order(order_id)

        if success:
            print(f"   [MOCK] cancel: {wallet_id} order={order_id}")
            return ExecutionOutcome(
                success=True,
                order_id=order_id,
                note="mock_cancel",
            )
        else:
            return ExecutionOutcome(
                success=False,
                error=f"Order not found: {order_id}",
            )

    def fetch_order_status(self, order_id: str, wallet) -> ExecutionOutcome:
        """查询订单状态（Mock）"""
        wallet_id = getattr(wallet, 'wallet_id', str(wallet))
        order = self.registry.get_order(order_id)

        if not order:
            return ExecutionOutcome(
                success=False,
                error=f"Order not found: {order_id}",
            )

        # 返回模拟的订单状态
        return ExecutionOutcome(
            success=True,
            order_id=order_id,
            price=0.5,
            shares=1.0,
            filled_shares=float(order.get("filled_shares", 0.0)),
            filled_amount_usd=float(order.get("filled_amount_usd", 0.0)),
            raw={
                "status": order["status"],
                "raw_status": order["status"],
                "filled_shares": order.get("filled_shares", 0.0),
                "filled_amount_usd": order.get("filled_amount_usd", 0.0),
            },
            note="mock_fetch_status",
        )


def create_mock_executor(
    fill_side: str = "UP",
    fill_after_sec: float = 5.0,
    fill_immediately: bool = False,
) -> tuple[MockOrderExecutor, MockOrderRegistry]:
    """
    创建 Mock 执行器的便捷函数

    Returns:
        (MockOrderExecutor, MockOrderRegistry)
    """
    config = MockOrderConfig(
        fill_side=fill_side,
        fill_after_sec=fill_after_sec,
        fill_immediately=fill_immediately,
    )
    executor = MockOrderExecutor(config)
    return executor, executor.registry
