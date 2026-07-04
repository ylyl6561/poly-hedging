"""
订单操作执行器 V2 - OrderExecutorV2

这是一个抽象了所有 Polymarket 订单操作的公共类，包括：
1. 挂初始买单 (GTC)
2. 撤单
3. 挂抛售单 (GTC)
4. 执行强平 (FAK)
5. 查询订单状态
6. 获取钱包余额

这个类与 TaskManager 解耦，可以在任何需要订单操作的地方使用。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from api import get_wallet_usdc_balance
from strategy.dual_wallet_models import (
    OrderSide,
    OrderSnapshot,
    OrderStatus,
    OperationType,
    WalletIdentity,
)
from strategy.event_task import coalesce_filled_shares
from strategy.status import CLOB_FILLED_STATUSES


class ExecutionOutcome:
    """订单执行结果。"""

    def __init__(
        self,
        success: bool,
        order_id: str | None = None,
        price: float | None = None,
        shares: float | None = None,
        filled_shares: float | None = None,
        filled_amount_usd: float | None = None,
        average_fill_price: float | None = None,
        raw_status: str | None = None,
        error: str | None = None,
        note: str | None = None,
        raw: Any = None,
        created_at: str | None = None,
    ):
        self.success = success
        self.order_id = order_id
        self.price = price
        self.shares = shares
        self.filled_shares = filled_shares
        self.filled_amount_usd = filled_amount_usd
        self.average_fill_price = average_fill_price
        self.raw_status = raw_status
        self.error = error
        self.note = note
        self.raw = raw
        self.created_at = created_at  # 链上订单创建时间


class OrderExecutorProtocol(Protocol):
    """订单执行器协议（定义 DualWalletExecutor 应实现的接口）。"""

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
        """挂初始买单。"""
        ...

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
        """挂抛售单或强平单。"""
        ...

    def cancel(self, order_id: str | None, wallet) -> ExecutionOutcome:
        """撤单。"""
        ...

    def fetch_order_status(self, order_id: str, wallet) -> ExecutionOutcome:
        """查询订单状态。"""
        ...


@dataclass
class OrderOperationResult:
    """订单操作结果（带快照）。"""
    outcome: ExecutionOutcome
    snapshot: OrderSnapshot | None = None


class OrderExecutorV2:
    """
    订单操作执行器 V2。

    提供标准化的 Polymarket 订单操作方法，支持：
    - GTC 买单（初始挂单）
    - GTC 卖单（抛售单）
    - FAK 卖单（强平单）
    - 撤单
    - 订单状态查询
    - 钱包余额查询
    """

    def __init__(
        self,
        executor: OrderExecutorProtocol,
        fixed_sell_price: float = 0.76,
        dry_run: bool = False,
    ):
        self.executor = executor
        self.fixed_sell_price = fixed_sell_price
        self.dry_run = dry_run

    # ===== 核心操作方法 =====

    def place_entry_order(
        self,
        wallet: WalletIdentity,
        event_name: str,
        side: OrderSide,
        amount_usd: float,
        price: float,
        clob_token_ids: list[str],
        fee_rate_bps: int,
        condition_id: str,
    ) -> OrderOperationResult:
        """
        挂初始买单（GTC）。

        Args:
            wallet: 钱包配置
            event_name: 事件名称
            side: 交易方向 (UP/DOWN)
            amount_usd: 挂单金额（USD）
            price: 挂单价格
            clob_token_ids: YES/NO token IDs
            fee_rate_bps: 费率（基点）
            condition_id: 条件 ID

        Returns:
            OrderOperationResult: 包含执行结果和订单快照
        """
        outcome = self.executor.place_entry(
            wallet=wallet,
            event_name=event_name,
            side=side,
            amount_usd=amount_usd,
            price=price,
            clob_token_ids=clob_token_ids,
            fee_rate_bps=fee_rate_bps,
            condition_id=condition_id,
            order_type_override="GTC",
            post_only=False,
        )

        snapshot = self._build_snapshot(
            wallet=wallet,
            event_name=event_name,
            side=side,
            amount_usd=amount_usd,
            operation=OperationType.PLACE,
            outcome=outcome,
        )

        return OrderOperationResult(outcome=outcome, snapshot=snapshot)

    def place_gtc_sell_order(
        self,
        wallet: WalletIdentity,
        event_name: str,
        side: OrderSide,
        shares: float,
        clob_token_ids: list[str],
        fee_rate_bps: int,
        condition_id: str,
        price: float | None = None,
    ) -> OrderOperationResult:
        """
        挂 GTC 抛售单。

        抛售单挂在订单簿上供市场在 entry_timeout → close_window 区间慢慢吃掉。
        不立即 FAK 强平，给市场一个软平仓的机会。

        Args:
            wallet: 钱包配置
            event_name: 事件名称
            side: 交易方向（与买入侧相反）
            shares: 抛售份额（token 数量）
            clob_token_ids: YES/NO token IDs
            fee_rate_bps: 费率（基点）
            condition_id: 条件 ID
            price: 抛售价格（默认使用 fixed_sell_price）

        Returns:
            OrderOperationResult: 包含执行结果和订单快照
        """
        sell_price = price if price is not None else self.fixed_sell_price
        sell_amount_usd = shares * sell_price

        outcome = self.executor.place_sell(
            wallet=wallet,
            event_name=event_name,
            side=side,
            shares=shares,
            price=sell_price,
            clob_token_ids=clob_token_ids,
            fee_rate_bps=fee_rate_bps,
            condition_id=condition_id,
            order_type_override="GTC",
            post_only=False,
        )

        snapshot = self._build_snapshot(
            wallet=wallet,
            event_name=event_name,
            side=side,
            amount_usd=sell_amount_usd,
            operation=OperationType.SELL,
            outcome=outcome,
            close_price=sell_price,
        )

        return OrderOperationResult(outcome=outcome, snapshot=snapshot)

    def place_fak_close_order(
        self,
        wallet: WalletIdentity,
        event_name: str,
        side: OrderSide,
        shares: float,
        clob_token_ids: list[str],
        fee_rate_bps: int,
        condition_id: str,
        price: float | None = None,
    ) -> OrderOperationResult:
        """
        挂 FAK 强平单。

        FAK (Fill-Or-Kill) 立即成交或取消，不会在订单簿上停留。
        用于事件结束前的强制平仓。

        Args:
            wallet: 钱包配置
            event_name: 事件名称
            side: 交易方向
            shares: 平仓份额（token 数量）
            clob_token_ids: YES/NO token IDs
            fee_rate_bps: 费率（基点）
            condition_id: 条件 ID
            price: 平仓价格（默认使用 fixed_sell_price）

        Returns:
            OrderOperationResult: 包含执行结果和订单快照
        """
        close_price = price if price is not None else self.fixed_sell_price
        close_amount_usd = shares * close_price

        outcome = self.executor.place_sell(
            wallet=wallet,
            event_name=event_name,
            side=side,
            shares=shares,
            price=close_price,
            clob_token_ids=clob_token_ids,
            fee_rate_bps=fee_rate_bps,
            condition_id=condition_id,
            order_type_override="FAK",
            post_only=False,
        )

        snapshot = self._build_snapshot(
            wallet=wallet,
            event_name=event_name,
            side=side,
            amount_usd=close_amount_usd,
            operation=OperationType.FORCE_CLOSE,
            outcome=outcome,
            close_price=close_price,
        )

        return OrderOperationResult(outcome=outcome, snapshot=snapshot)

    def cancel_order(
        self,
        order_id: str | None,
        wallet: WalletIdentity,
        event_name: str,
        side: OrderSide | None,
        amount_usd: float = 0.0,
    ) -> OrderOperationResult:
        """
        撤单。

        Args:
            order_id: 订单 ID
            wallet: 钱包配置
            event_name: 事件名称
            side: 交易方向
            amount_usd: 订单金额

        Returns:
            OrderOperationResult: 包含执行结果和订单快照
        """
        outcome = self.executor.cancel(order_id, wallet=wallet)

        snapshot = self._build_snapshot(
            wallet=wallet,
            event_name=event_name,
            side=side,
            amount_usd=amount_usd,
            operation=OperationType.CANCEL,
            outcome=outcome,
        )

        return OrderOperationResult(outcome=outcome, snapshot=snapshot)

    def refresh_order_status(
        self,
        order_id: str,
        wallet: WalletIdentity,
    ) -> tuple[ExecutionOutcome, OrderSnapshot | None]:
        """
        查询并刷新订单状态。

        Args:
            order_id: 订单 ID
            wallet: 钱包配置

        Returns:
            (ExecutionOutcome, OrderSnapshot): 执行结果和更新后的快照
        """
        outcome = self.executor.fetch_order_status(order_id, wallet=wallet)

        # 构建更新后的快照（如果原快照存在）
        snapshot = None
        if outcome.success and isinstance(outcome.raw, dict):
            raw = outcome.raw
            status = str(raw.get("status") or "").lower()

            # 归一化状态
            from strategy.status import normalize_clob_status
            normalized_status = normalize_clob_status(status)

            # 【修复说明】
            # 问题：OrderSnapshot.timestamp 使用本地时间而非链上时间
            # 修复：优先使用 API 返回的链上 created_at 时间
            timestamp = datetime.now(timezone.utc)
            if outcome.created_at:
                try:
                    # API 返回的是 ISO 格式字符串
                    timestamp = datetime.fromisoformat(outcome.created_at.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    # 解析失败，使用本地时间
                    pass

            snapshot = OrderSnapshot(
                wallet=wallet,
                event_name="",  # 调用方需要补充
                side=OrderSide.UP,  # 调用方需要补充
                amount_usd=0.0,
                operation=OperationType.PLACE,
                order_id=order_id,
                status=normalized_status,
                raw_status=raw.get("raw_status") or raw.get("status"),
                shares=outcome.shares,
                filled_shares=outcome.filled_shares,
                filled_amount_usd=outcome.filled_amount_usd,
                average_fill_price=outcome.average_fill_price,
                error=outcome.error,
                timestamp=timestamp,  # 使用链上时间
            )

        return outcome, snapshot

    def get_wallet_balance(self, wallet: WalletIdentity) -> float | None:
        """
        获取钱包 USDC 余额。

        Args:
            wallet: 钱包配置

        Returns:
            float | None: USDC 余额，失败返回 None
        """
        try:
            payload = get_wallet_usdc_balance(account=wallet.account)
            if isinstance(payload, dict):
                balance = payload.get("balance_usdc")
                return float(balance) if balance is not None else None
        except Exception:
            pass
        return None

    def get_all_wallet_balances(
        self,
        wallets: list[WalletIdentity],
    ) -> dict[str, float | None]:
        """
        获取多个钱包的 USDC 余额。

        Args:
            wallets: 钱包配置列表

        Returns:
            dict[str, float | None]: wallet_id -> 余额
        """
        return {
            wallet.wallet_id: self.get_wallet_balance(wallet)
            for wallet in wallets
        }

    # ===== 组合操作 =====

    def handle_single_side_fill(
        self,
        wallet: WalletIdentity,
        event_name: str,
        side: OrderSide,
        filled_shares: float,
        filled_amount_usd: float,
        stale_wallet: WalletIdentity,
        stale_side: OrderSide,
        stale_order_id: str | None,
        stale_amount_usd: float,
        clob_token_ids: list[str],
        fee_rate_bps: int,
        condition_id: str,
    ) -> tuple[OrderOperationResult, OrderOperationResult, OrderOperationResult]:
        """
        处理单边成交：撤另一侧 + 挂 GTC 抛售单。

        这是完整的单边成交处理流程：
        1. 撤 stale 侧挂单
        2. 刷新 stale 侧订单状态（检测是否在撤单前已成交）
        3. 对 live 侧挂 GTC 抛售单

        Args:
            wallet: 已成交侧钱包
            event_name: 事件名称
            side: 已成交侧方向
            filled_shares: 已成交份额
            filled_amount_usd: 已成交金额
            stale_wallet: 未成交侧钱包
            stale_side: 未成交侧方向
            stale_order_id: 未成交侧订单 ID
            stale_amount_usd: 未成交侧订单金额
            clob_token_ids: YES/NO token IDs
            fee_rate_bps: 费率
            condition_id: 条件 ID

        Returns:
            (cancel_result, cancel_refresh_result, sell_result):
            - cancel_result: 撤单结果
            - cancel_refresh_result: 撤单后刷新结果（检测是否已成交）
            - sell_result: 抛售单结果
        """
        # 1. 尝试撤单
        cancel_result = self.cancel_order(
            order_id=stale_order_id,
            wallet=stale_wallet,
            event_name=event_name,
            side=stale_side,
            amount_usd=stale_amount_usd,
        )

        # 2. 撤单后检查实际状态（可能撤单 API 失败但订单实际已成交）
        cancel_refresh_result = OrderOperationResult(
            outcome=ExecutionOutcome(success=False),
            snapshot=None,
        )
        if not cancel_result.outcome.success and stale_order_id:
            refresh_outcome, refresh_snapshot = self.refresh_order_status(
                order_id=stale_order_id,
                wallet=stale_wallet,
            )
            if refresh_outcome.success and isinstance(refresh_outcome.raw, dict):
                latest_status = str(refresh_outcome.raw.get("status") or "").lower()
                if latest_status in CLOB_FILLED_STATUSES:
                    # 订单实际已成交，视为撤单成功
                    cancel_refresh_result = OrderOperationResult(
                        outcome=ExecutionOutcome(
                            success=True,
                            order_id=stale_order_id,
                            note=f"benign_terminal_via_refresh:{latest_status}",
                            raw=refresh_outcome.raw,
                        ),
                        snapshot=refresh_snapshot,
                    )

        # 3. 挂 GTC 抛售单
        # 计算抛售份额
        sell_shares = filled_shares
        if sell_shares <= 0 and filled_amount_usd > 0:
            sell_shares = filled_amount_usd / self.fixed_sell_price

        sell_result = self.place_gtc_sell_order(
            wallet=wallet,
            event_name=event_name,
            side=side,
            shares=sell_shares,
            clob_token_ids=clob_token_ids,
            fee_rate_bps=fee_rate_bps,
            condition_id=condition_id,
        )

        return cancel_result, cancel_refresh_result, sell_result

    def execute_force_close(
        self,
        wallet: WalletIdentity,
        event_name: str,
        side: OrderSide,
        shares: float,
        clob_token_ids: list[str],
        fee_rate_bps: int,
        condition_id: str,
    ) -> OrderOperationResult:
        """
        执行 FAK 强平。

        这是最终的强制平仓操作，在事件结束前执行。
        使用 FAK 立即成交或取消。

        Args:
            wallet: 钱包配置
            event_name: 事件名称
            side: 交易方向
            shares: 平仓份额
            clob_token_ids: YES/NO token IDs
            fee_rate_bps: 费率
            condition_id: 条件 ID

        Returns:
            OrderOperationResult: 包含执行结果和订单快照
        """
        return self.place_fak_close_order(
            wallet=wallet,
            event_name=event_name,
            side=side,
            shares=shares,
            clob_token_ids=clob_token_ids,
            fee_rate_bps=fee_rate_bps,
            condition_id=condition_id,
        )

    # ===== 辅助方法 =====

    @staticmethod
    def _build_snapshot(
        wallet: WalletIdentity,
        event_name: str,
        side: OrderSide | None,
        amount_usd: float,
        operation: OperationType,
        outcome: ExecutionOutcome,
        close_price: float | None = None,
    ) -> OrderSnapshot:
        """构建订单快照。"""
        return OrderSnapshot(
            wallet=wallet,
            event_name=event_name,
            side=side or OrderSide.UP,
            amount_usd=amount_usd,
            operation=operation,
            timestamp=datetime.now(timezone.utc),
            order_id=outcome.order_id,
            token_id=None,
            condition_id=None,
            price=outcome.price,
            shares=outcome.shares,
            status=OrderStatus.SUBMITTED.value if outcome.success else OrderStatus.FAILED.value,
            close_price=close_price,
            filled_amount_usd=outcome.filled_amount_usd,
            filled_shares=outcome.filled_shares,
            average_fill_price=outcome.average_fill_price,
            error=outcome.error,
        )

    @staticmethod
    def is_order_filled(snapshot: OrderSnapshot | None) -> bool:
        """判断订单是否已成交。"""
        if not snapshot:
            return False
        if snapshot.status != OrderStatus.FILLED.value:
            return False
        # 区分 "未填" (None) 和 "成交为 0"：必须 > 0 才算真成交
        return coalesce_filled_shares(snapshot.filled_shares, snapshot.shares) > 0

    @staticmethod
    def is_order_submitted(snapshot: OrderSnapshot | None) -> bool:
        """判断订单是否在簿上（SUBMITTED）。"""
        if not snapshot:
            return False
        return snapshot.status == OrderStatus.SUBMITTED.value

    @staticmethod
    def get_filled_shares(snapshot: OrderSnapshot | None) -> float:
        """获取成交份额。"""
        if not snapshot:
            return 0.0
        return coalesce_filled_shares(snapshot.filled_shares, snapshot.shares)

    def format_operation_log(
        self,
        wallet: WalletIdentity,
        operation: str,
        side: OrderSide | None,
        amount_usd: float,
        snapshot: OrderSnapshot | None,
    ) -> str:
        """格式化操作日志。"""
        side_text = side.value if side else "UNKNOWN"
        price_text = f"{snapshot.price:.4f}" if snapshot and snapshot.price else "UNKNOWN"
        shares_text = f"{snapshot.shares:.4f}" if snapshot and snapshot.shares else "UNKNOWN"
        status_text = snapshot.status if snapshot else "UNKNOWN"
        error_text = snapshot.error if snapshot else ""
        order_id_text = snapshot.order_id if snapshot else "UNKNOWN"

        return (
            f"【{operation}】{wallet.wallet_name}({wallet.account.account_id})"
            f" | side={side_text}"
            f" | amount_usd={amount_usd:.4f}"
            f" | price={price_text}"
            f" | shares={shares_text}"
            f" | status={status_text}"
            f" | error={error_text}"
            f" | order_id={order_id_text}"
        )
