"""
Execution adapters for the dual-wallet event strategy.

This module wraps the existing API/trading layer and exposes a small set of
wallet-aware operations. It deliberately keeps raw Polymarket details outside
of the strategy state machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from api import direct_polymarket_trade, cancel_order, fetch_side_orderbook_price

from .dual_wallet_models import WalletIdentity, OrderSide, OperationType, OrderSnapshot


@dataclass
class ExecutionOutcome:
    success: bool
    order_id: str | None = None
    price: float | None = None
    shares: float | None = None
    error: str | None = None
    raw: dict[str, Any] | None = None


class DualWalletExecutor:
    def __init__(self, *, dry_run: bool):
        self.dry_run = dry_run

    def place_entry(
        self,
        *,
        wallet: WalletIdentity,
        event_name: str,
        side: OrderSide,
        amount_usd: float,
        price: float,
        clob_token_ids: list[str],
        fee_rate_bps: int,
        condition_id: str,
        order_type_override: str = "GTC",
        post_only: bool = False,
    ) -> ExecutionOutcome:
        result = direct_polymarket_trade(
            side=side.value.lower(),
            amount=amount_usd,
            price=price,
            clob_token_ids=clob_token_ids,
            fee_rate_bps=fee_rate_bps,
            condition_id=condition_id,
            order_type_override=order_type_override,
            post_only=post_only,
            mock=self.dry_run,
        )
        return self._normalize_result(result, fallback_price=price, mock=self.dry_run, wallet=wallet, side=side, condition_id=condition_id, event_name=event_name, amount_usd=amount_usd)

    def cancel(self, order_id: str | None) -> ExecutionOutcome:
        if not order_id:
            return ExecutionOutcome(success=False, error="missing_order_id")
        result = cancel_order(order_id, mock=self.dry_run)
        if isinstance(result, dict) and result.get("success"):
            return ExecutionOutcome(success=True, order_id=order_id, raw=result)
        return ExecutionOutcome(success=False, order_id=order_id, error=(result or {}).get("error") if isinstance(result, dict) else str(result), raw=result if isinstance(result, dict) else None)

    def fetch_best_ask(self, clob_token_ids: list[str], side: OrderSide) -> float | None:
        data = fetch_side_orderbook_price(clob_token_ids, side.value.lower())
        if not data:
            return None
        return data.get("best_ask")

    def _normalize_result(self, result: dict[str, Any] | Any, *, fallback_price: float | None, mock: bool = False, wallet: WalletIdentity | None = None, side: OrderSide | None = None, condition_id: str | None = None, event_name: str | None = None, amount_usd: float | None = None) -> ExecutionOutcome:
        if not isinstance(result, dict):
            return ExecutionOutcome(success=False, error=str(result), raw=None)
        if result.get("success"):
            fill_price = result.get("fill_price")
            if fill_price is None:
                fill_price = fallback_price
            shares = result.get("shares") or result.get("shares_bought")
            return ExecutionOutcome(
                success=True,
                order_id=result.get("trade_id") or result.get("order_id") or (f"MOCK-{wallet.wallet_id}-{side.value}-{(condition_id or '')[:8]}" if mock and wallet and side else None),
                price=fill_price,
                shares=shares if shares is not None else ((amount_usd / fill_price) if mock and amount_usd and fill_price else None),
                raw=result,
            )
        return ExecutionOutcome(success=False, error=result.get("error") or result.get("skip_reason") or "unknown_error", raw=result)


def build_order_snapshot(
    *,
    wallet: WalletIdentity,
    event_name: str,
    side: OrderSide,
    amount_usd: float,
    operation: OperationType,
    outcome: ExecutionOutcome,
    close_price: float | None = None,
) -> OrderSnapshot:
    return OrderSnapshot(
        wallet=wallet,
        event_name=event_name,
        side=side,
        amount_usd=amount_usd,
        operation=operation,
        order_id=outcome.order_id,
        price=outcome.price,
        shares=outcome.shares,
        status="filled" if outcome.success else "failed",
        close_price=close_price,
    )
