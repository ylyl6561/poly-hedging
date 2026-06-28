"""
Execution adapters for the dual-wallet event strategy.

This module wraps the existing API/trading layer and exposes a small set of
wallet-aware operations. It deliberately keeps raw Polymarket details outside
of the strategy state machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from collections import defaultdict

from api import direct_polymarket_trade, cancel_order, fetch_order_status, fetch_side_orderbook_price

from .dual_wallet_models import WalletIdentity, OrderSide, OperationType, OrderSnapshot, OrderStatus
from .event_task import coalesce_filled_shares


@dataclass
class ExecutionOutcome:
    success: bool
    order_id: str | None = None
    token_id: str | None = None
    condition_id: str | None = None
    price: float | None = None
    shares: float | None = None
    filled_amount_usd: float | None = None
    filled_shares: float | None = None
    average_fill_price: float | None = None
    raw_status: str | None = None
    error: str | None = None
    note: str | None = None
    raw: dict[str, Any] | None = None


class DualWalletExecutor:
    def __init__(self, *, dry_run: bool, dry_run_status_script: dict[str, Any] | None = None):
        self.dry_run = dry_run
        self.dry_run_status_script = dry_run_status_script if isinstance(dry_run_status_script, dict) else {}
        self._dry_run_status_cursor: dict[str, int] = defaultdict(int)

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
            account=wallet.account,
            trade_side="buy",
        )
        return self._normalize_result(result, fallback_price=price, mock=self.dry_run, wallet=wallet, side=side, condition_id=condition_id, event_name=event_name, amount_usd=amount_usd)

    def place_sell(
        self,
        *,
        wallet: WalletIdentity,
        event_name: str,
        side: OrderSide,
        shares: float,
        price: float,
        clob_token_ids: list[str],
        fee_rate_bps: int,
        condition_id: str,
        order_type_override: str = "GTC",
        post_only: bool = False,
    ) -> ExecutionOutcome:
        """Place a SELL order to close/reduce an existing position.

        `shares` is the token count to sell (NOT USD). The function passes it as
        the `amount` argument to `direct_polymarket_trade` with `trade_side="sell"`.
        For SELL market orders, Polymarket V2 MarketOrderArgs.amount is the token
        count, not USD. For SELL limit orders, OrderArgs.size is the share count.
        """
        if not shares or shares <= 0:
            return ExecutionOutcome(success=False, error="invalid_share_count")
        result = direct_polymarket_trade(
            side=side.value.lower(),
            amount=float(shares),
            price=price,
            clob_token_ids=clob_token_ids,
            fee_rate_bps=fee_rate_bps,
            condition_id=condition_id,
            order_type_override=order_type_override,
            post_only=post_only,
            mock=self.dry_run,
            account=wallet.account,
            trade_side="sell",
        )
        # For SELL, `amount_usd` in the outcome is the notional USD we'll receive
        # at the limit price (or the limit-price reference for fills).
        notional_usd = float(shares) * float(price) if price else None
        return self._normalize_result(
            result,
            fallback_price=price,
            mock=self.dry_run,
            wallet=wallet,
            side=side,
            condition_id=condition_id,
            event_name=event_name,
            amount_usd=notional_usd,
            trade_side="sell",
            shares=shares,
        )

    def cancel(self, order_id: str | None, *, wallet: WalletIdentity | None = None) -> ExecutionOutcome:
        if not order_id:
            return ExecutionOutcome(success=False, error="missing_order_id")
        result = cancel_order(order_id, mock=self.dry_run, account=wallet.account if wallet else None)
        if isinstance(result, dict) and result.get("success"):
            return ExecutionOutcome(success=True, order_id=order_id, raw=result)
        return ExecutionOutcome(success=False, order_id=order_id, error=(result or {}).get("error") if isinstance(result, dict) else str(result), raw=result if isinstance(result, dict) else None)

    def fetch_best_ask(self, clob_token_ids: list[str], side: OrderSide) -> float | None:
        data = fetch_side_orderbook_price(clob_token_ids, side.value.lower())
        if not data:
            return None
        return data.get("best_ask")

    def fetch_order_status(self, order_id: str | None, *, wallet: WalletIdentity | None = None) -> ExecutionOutcome:
        if not order_id:
            return ExecutionOutcome(success=False, error="missing_order_id")
        scripted = self._get_dry_run_scripted_status(order_id=order_id, wallet=wallet)
        if scripted is not None:
            raw_filled = scripted.get("filled_shares")
            raw_shares = scripted.get("shares")
            return ExecutionOutcome(
                success=True,
                order_id=order_id,
                price=scripted.get("price"),
                shares=raw_shares,
                filled_amount_usd=scripted.get("filled_amount_usd"),
                # 关键：0 (成交为 0) 必须透传，不能用 or 把 0 当成 falsy 吞掉。
                filled_shares=coalesce_filled_shares(raw_filled, raw_shares),
                average_fill_price=scripted.get("average_fill_price") or scripted.get("price"),
                raw_status=scripted.get("raw_status"),
                raw=scripted,
            )
        result = fetch_order_status(order_id, mock=self.dry_run, account=wallet.account if wallet else None)
        if isinstance(result, dict) and result.get("success"):
            raw_filled = result.get("filled_shares")
            raw_shares = result.get("shares")
            return ExecutionOutcome(
                success=True,
                order_id=order_id,
                price=result.get("price"),
                shares=raw_shares,
                filled_amount_usd=result.get("filled_amount_usd"),
                # 关键：0 (成交为 0) 必须透传，不能用 or 把 0 当成 falsy 吞掉。
                filled_shares=coalesce_filled_shares(raw_filled, raw_shares),
                average_fill_price=result.get("average_fill_price") or result.get("price"),
                raw_status=result.get("raw_status"),
                raw=result,
            )
        return ExecutionOutcome(success=False, order_id=order_id, error=(result or {}).get("error") if isinstance(result, dict) else str(result), raw=result if isinstance(result, dict) else None)

    def _get_dry_run_scripted_status(self, *, order_id: str, wallet: WalletIdentity | None) -> dict[str, Any] | None:
        if not self.dry_run or not wallet or not self.dry_run_status_script:
            return None
        wallet_script = self.dry_run_status_script.get(wallet.wallet_id)
        if not isinstance(wallet_script, dict):
            return None
        sequences = wallet_script.get("by_side") if isinstance(wallet_script.get("by_side"), dict) else wallet_script
        side_key = None
        order_id_lower = order_id.lower() if order_id else ""
        for candidate in ("UP", "DOWN"):
            if f"-{candidate.lower()}-" in order_id_lower:
                side_key = candidate
                break
        sequence = sequences.get(side_key) if side_key and isinstance(sequences, dict) else None
        if not isinstance(sequence, list) or not sequence:
            return None
        cursor_key = f"{wallet.wallet_id}:{side_key or 'UNKNOWN'}"
        index = min(self._dry_run_status_cursor[cursor_key], len(sequence) - 1)
        self._dry_run_status_cursor[cursor_key] += 1
        status_name = str(sequence[index]).lower()
        if status_name == "filled":
            price = float(wallet_script.get("fill_price", 0.5) or 0.5)
            amount = float(wallet_script.get("filled_amount_usd", 10.0) or 10.0)
            shares = float(wallet_script.get("filled_shares", (amount / price if price else 0.0)) or 0.0)
            return {
                "success": True,
                "status": "filled",
                "price": price,
                "shares": shares,
                "filled_shares": shares,
                "filled_amount_usd": amount,
                "average_fill_price": price,
                "raw_status": "filled",
                "scripted": True,
            }
        if status_name == "cancelled":
            return {"success": True, "status": "cancelled", "raw_status": "cancelled", "scripted": True}
        if status_name == "failed":
            return {"success": True, "status": "failed", "raw_status": "failed", "scripted": True}
        return {"success": True, "status": "submitted", "raw_status": "submitted", "scripted": True}

    def _normalize_result(self, result: dict[str, Any] | Any, *, fallback_price: float | None, mock: bool = False, wallet: WalletIdentity | None = None, side: OrderSide | None = None, condition_id: str | None = None, event_name: str | None = None, amount_usd: float | None = None, trade_side: str = "buy", shares: float | None = None) -> ExecutionOutcome:
        if not isinstance(result, dict):
            return ExecutionOutcome(success=False, error=str(result), raw=None)
        if result.get("success"):
            fill_price = result.get("fill_price")
            if fill_price is None:
                fill_price = fallback_price
            # SELL responses have shares_sold; BUY responses have shares_bought.
            result_shares = result.get("shares") or result.get("shares_sold") or result.get("shares_bought")
            if result_shares is not None:
                resolved_shares = result_shares
            elif trade_side == "sell" and shares is not None:
                resolved_shares = float(shares)
            elif mock and amount_usd and fill_price:
                resolved_shares = amount_usd / fill_price
            else:
                resolved_shares = None
            return ExecutionOutcome(
                success=True,
                order_id=result.get("trade_id") or result.get("order_id") or (f"MOCK-{wallet.wallet_id}-{side.value}-{(condition_id or '')[:8]}" if mock and wallet and side else None),
                token_id=(result.get("token_id") or result.get("asset_id") or result.get("market_asset_id") or (result.get("raw", {}) if isinstance(result.get("raw"), dict) else {}).get("token_id")),
                condition_id=condition_id,
                price=fill_price,
                shares=resolved_shares,
                filled_amount_usd=float(amount_usd) if amount_usd is not None else None,
                filled_shares=resolved_shares,
                average_fill_price=fill_price,
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
        token_id=outcome.token_id,
        condition_id=outcome.condition_id,
        price=outcome.price,
        shares=outcome.shares,
        status=OrderStatus.SUBMITTED.value if outcome.success and operation == OperationType.PLACE else (OrderStatus.FILLED.value if outcome.success else OrderStatus.FAILED.value),
        close_price=close_price,
        filled_amount_usd=outcome.filled_amount_usd,
        filled_shares=outcome.filled_shares,
        average_fill_price=outcome.average_fill_price,
        raw_status=outcome.raw_status,
        error=outcome.error,
    )
