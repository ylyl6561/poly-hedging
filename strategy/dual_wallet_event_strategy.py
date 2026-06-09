"""Dual-wallet event strategy orchestration."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from accounts import get_account_registry
from api import fetch_market_outcome, get_wallet_usdc_balance
from core import resolve_config
from notifications.feishu_tools import send_feishu
from state.reconcile_export import export_dual_wallet_event_to_excel
from state.structured_log import StructuredRunLog

from .account_pool import AccountPool
from .dual_wallet_executor import DualWalletExecutor, build_order_snapshot, ExecutionOutcome
from .dual_wallet_models import (
    DualWalletEventState,
    EventFlowState,
    EventOutcome,
    EventResultSummary,
    LossWindowTracker,
    OrderSide,
    OrderStatus,
    OperationType,
    assign_event_sides,
    build_wallet_identities,
)


class DualWalletEventStrategy:
    def __init__(self, *, run_folder, dry_run: bool, config: dict[str, Any] | None = None, structured_log: StructuredRunLog | None = None):
        self.run_folder = run_folder
        self.dry_run = dry_run
        self.config = config or resolve_config(__file__)
        self.structured_log = structured_log
        self.executor = DualWalletExecutor(dry_run=dry_run)
        registry = get_account_registry()
        self.account_pool = AccountPool(registry.list_accounts())
        self.selected_accounts = self._select_two_dual_wallet_accounts()
        self.wallets = build_wallet_identities(self.selected_accounts)
        self.entry_timeout_sec = int(self.config.get("dual_wallet_entry_timeout_sec", 100))
        self.force_close_window_sec = int(self.config.get("dual_wallet_force_close_window_sec", 40))
        self.fixed_sell_price = float(self.config.get("dual_wallet_fixed_sell_price", 0.76))
        self.max_consecutive_losses = int(self.config.get("dual_wallet_max_consecutive_losses", 2))
        self.poll_interval_sec = int(self.config.get("dual_wallet_poll_interval_sec", 5))
        self.outcome_poll_interval_sec = int(self.config.get("dual_wallet_outcome_poll_interval_sec", 5))
        self.outcome_poll_timeout_sec = int(self.config.get("dual_wallet_outcome_poll_timeout_sec", 900))
        self.loss_window = LossWindowTracker()
        self._halted = False
        self._halt_reason: str | None = None

    def should_halt(self) -> bool:
        return self._halted

    def _select_two_dual_wallet_accounts(self):
        tagged_accounts = self.account_pool.accounts_with_tag("dual_wallet")
        if len(tagged_accounts) >= 2:
            return tagged_accounts[:2]
        return self.account_pool.require_accounts(2)

    def _build_selected_account_payload(self) -> list[dict[str, str]]:
        return [
            {
                "account_id": account.account_id,
                "label": account.label,
                "wallet_address": account.wallet_address,
            }
            for account in self.selected_accounts
        ]

    def _build_side_assignment_payload(self, state: DualWalletEventState) -> list[dict[str, str]]:
        payload: list[dict[str, str]] = []
        for wallet in self.wallets:
            side = state.side_by_wallet_id.get(wallet.wallet_id)
            payload.append(
                {
                    "account_id": wallet.account.account_id,
                    "label": wallet.account.label,
                    "wallet_address": wallet.account.wallet_address,
                    "role": wallet.role.value,
                    "side": side.value if side else "UNKNOWN",
                }
            )
        return payload

    def halt_reason(self) -> str | None:
        return self._halt_reason

    def run_event(
        self,
        *,
        event_name: str,
        event_id: str,
        start_time: datetime,
        end_time: datetime,
        clob_token_ids: list[str],
        fee_rate_bps: int,
        condition_id: str,
        amount_usd: float,
        up_price: float,
        down_price: float,
    ) -> EventResultSummary:
        if self._halted:
            return EventResultSummary(event_name=event_name, outcome=EventOutcome.UNKNOWN, is_profit=False)
        if datetime.now(timezone.utc) >= start_time:
            print(f"【跳过】{event_name}：当前事件已开始，只允许下单接下来未开始的事件")
            return EventResultSummary(event_name=event_name, outcome=EventOutcome.UNKNOWN, is_profit=False)

        side_by_wallet_id, first_wallet_is_up = assign_event_sides(self.wallets)
        state = DualWalletEventState(
            event_name=event_name,
            event_id=event_id,
            start_time=start_time,
            end_time=end_time,
            close_price=self.fixed_sell_price,
            close_window_sec=self.force_close_window_sec,
            x_timeout_sec=self.entry_timeout_sec,
            side_by_wallet_id=side_by_wallet_id,
        )
        self._log_wallet_account_data(amount_usd=amount_usd)
        if not self._ensure_wallet_capacity(amount_usd=amount_usd):
            state.flow_state = EventFlowState.STOPPED
            state.halted_reason = self._halt_reason
            self._log_state(state, phase=state.flow_state.value, note=state.halted_reason)
            return EventResultSummary(event_name=event_name, outcome=EventOutcome.UNKNOWN, is_profit=False)
        self._announce_event(state, condition_id=condition_id, amount_usd=amount_usd, up_price=up_price, down_price=down_price)
        if self.structured_log:
            self.structured_log.record_event(
                event_name=state.event_name,
                event_id=state.event_id,
                phase="start",
                payload={
                    "condition_id": condition_id,
                    "start_time": state.start_time.isoformat(),
                    "end_time": state.end_time.isoformat(),
                    "selected_accounts": self._build_selected_account_payload(),
                    "side_assignment": self._build_side_assignment_payload(state),
                    "random_assignment": {
                        "first_wallet_role": self.wallets[0].role.value,
                        "first_wallet_is_up": first_wallet_is_up,
                    },
                },
            )
        self._place_initial_legs(
            state,
            clob_token_ids=clob_token_ids,
            fee_rate_bps=fee_rate_bps,
            condition_id=condition_id,
            amount_usd=amount_usd,
            up_price=up_price,
            down_price=down_price,
        )
        self._log_state(state, phase="entry_placed", note="initial legs submitted")
        self._wait_and_handle_partials(state, clob_token_ids=clob_token_ids, fee_rate_bps=fee_rate_bps, condition_id=condition_id)
        self._force_close_if_needed(state, clob_token_ids=clob_token_ids, fee_rate_bps=fee_rate_bps, condition_id=condition_id)
        summary = self._build_summary(state, condition_id=condition_id)
        state.result_summary = summary
        state.flow_state = EventFlowState.SETTLED
        self._log_state(state, phase=state.flow_state.value, note=f"settled outcome={summary.outcome.value}")
        self._update_stop_guard(summary)
        self._export_summary(state, summary)
        return summary

    def _log_state(self, state: DualWalletEventState, *, phase: str, note: str | None = None) -> None:
        if not self.structured_log:
            return
        self.structured_log.record_event_state(
            event_name=state.event_name,
            event_id=state.event_id,
            flow_state=phase,
            wallet_status={wallet_id: snapshot.status for wallet_id, snapshot in state.wallet_orders.items()},
            note=note,
            payload={
                "selected_accounts": self._build_selected_account_payload(),
                "side_assignment": self._build_side_assignment_payload(state),
            },
        )

    def _announce_event(self, state: DualWalletEventState, *, condition_id: str, amount_usd: float, up_price: float, down_price: float) -> None:
        print(f"【事件】{state.event_name}")
        print(f"【窗口】{state.start_time.astimezone(timezone.utc).isoformat()} UTC -> {state.end_time.astimezone(timezone.utc).isoformat()} UTC")
        print(f"【市场】condition_id={condition_id} | close_price={self.fixed_sell_price:.4f} | timeout={self.entry_timeout_sec}s")
        print(f"【初始挂单配置】amount_usd={float(amount_usd):.4f} | up_price={float(up_price):.4f} | down_price={float(down_price):.4f}")
        selected_accounts = [f"{wallet.wallet_name}({wallet.account.account_id})" for wallet in self.wallets]
        print(f"【选中账号】{' | '.join(selected_accounts)}")
        mapping = []
        for wallet in self.wallets:
            side = state.side_by_wallet_id.get(wallet.wallet_id)
            mapping.append(f"{wallet.wallet_name}({wallet.account.account_id})->{side.value if side else 'UNKNOWN'}")
        print(f"【本轮分配】{' | '.join(mapping)}")

    def _fetch_wallet_account_data(self) -> dict[str, dict[str, Any]]:
        account_data: dict[str, dict[str, Any]] = {}
        for wallet in self.wallets:
            payload = get_wallet_usdc_balance(account=wallet.account)
            account_data[wallet.wallet_id] = payload if isinstance(payload, dict) else {"success": False, "error": "invalid_balance_payload"}
        return account_data

    def _log_wallet_account_data(self, *, amount_usd: float) -> None:
        for wallet in self.wallets:
            payload = get_wallet_usdc_balance(account=wallet.account)
            balance = payload.get("balance_usdc") if isinstance(payload, dict) else None
            if isinstance(payload, dict) and payload.get("success") and balance is not None:
                print(f"【账户资金】{wallet.wallet_name}：available_usdc={float(balance):.4f} | next_order_amount={float(amount_usd):.4f}")
            else:
                error = payload.get("error") if isinstance(payload, dict) else "balance_unavailable"
                print(f"【账户资金】{wallet.wallet_name}：读取失败 | error={error}")

    def _ensure_wallet_capacity(self, *, amount_usd: float) -> bool:
        account_data = self._fetch_wallet_account_data()
        insufficient_wallets: list[str] = []
        for wallet in self.wallets:
            payload = account_data.get(wallet.wallet_id, {})
            balance = payload.get("balance_usdc") if isinstance(payload, dict) else None
            if not isinstance(payload, dict) or not payload.get("success") or balance is None:
                insufficient_wallets.append(f"{wallet.wallet_name}:balance_unavailable")
                continue
            try:
                if float(balance) + 1e-9 < float(amount_usd):
                    insufficient_wallets.append(f"{wallet.wallet_name}:need={float(amount_usd):.4f},available={float(balance):.4f}")
            except (TypeError, ValueError):
                insufficient_wallets.append(f"{wallet.wallet_name}:invalid_balance")
        if insufficient_wallets:
            mode = "dry_run" if self.dry_run else "live"
            self._halt_reason = f"insufficient_{mode}_balance | {'; '.join(insufficient_wallets)}"
            if not self.dry_run:
                self._halted = True
                print(f"【停机】{self._halt_reason}")
            else:
                print(f"【跳过】{self._halt_reason}")
            return False
        return True

    def _place_initial_legs(self, state: DualWalletEventState, *, clob_token_ids: list[str], fee_rate_bps: int, condition_id: str, amount_usd: float, up_price: float, down_price: float) -> None:
        for wallet in self.wallets:
            side = state.side_by_wallet_id.get(wallet.wallet_id)
            price = up_price if side == OrderSide.UP else down_price
            outcome = self.executor.place_entry(wallet=wallet, event_name=state.event_name, side=side, amount_usd=amount_usd, price=price, clob_token_ids=clob_token_ids, fee_rate_bps=fee_rate_bps, condition_id=condition_id, order_type_override="GTC", post_only=False)
            snapshot = build_order_snapshot(wallet=wallet, event_name=state.event_name, side=side, amount_usd=amount_usd, operation=OperationType.PLACE, outcome=outcome)
            state.mark_order(snapshot)
            self._log_order_submission(wallet=wallet, side=side, amount_usd=amount_usd, snapshot=snapshot)
        state.flow_state = EventFlowState.ENTRY_PLACED
        self._log_state(state, phase=state.flow_state.value, note="initial entries placed")

    def _log_order_submission(self, *, wallet, side: OrderSide | None, amount_usd: float, snapshot) -> None:
        side_text = side.value if side else "UNKNOWN"
        price_text = f"{float(snapshot.price):.4f}" if snapshot.price is not None else "UNKNOWN"
        shares_text = f"{float(snapshot.shares):.4f}" if snapshot.shares is not None else "UNKNOWN"
        order_id_text = snapshot.order_id or "UNKNOWN"
        token_id_text = snapshot.token_id or "UNKNOWN"
        condition_id_text = snapshot.condition_id or "UNKNOWN"
        status_text = snapshot.status or "UNKNOWN"
        print(
            f"【挂单】{wallet.wallet_name}({wallet.account.account_id})"
            f" | side={side_text}"
            f" | amount_usd={float(amount_usd):.4f}"
            f" | price={price_text}"
            f" | shares={shares_text}"
            f" | status={status_text}"
            f" | token_id={token_id_text}"
            f" | condition_id={condition_id_text}"
            f" | order_id={order_id_text}"
        )

    def _wait_and_handle_partials(self, state: DualWalletEventState, *, clob_token_ids: list[str], fee_rate_bps: int, condition_id: str) -> None:
        deadline = datetime.now(timezone.utc) + timedelta(seconds=self.entry_timeout_sec)
        while datetime.now(timezone.utc) < deadline:
            self._refresh_entry_order_statuses(state)
            up_wallet = next((wallet for wallet in self.wallets if state.side_by_wallet_id.get(wallet.wallet_id) == OrderSide.UP), None)
            down_wallet = next((wallet for wallet in self.wallets if state.side_by_wallet_id.get(wallet.wallet_id) == OrderSide.DOWN), None)
            up = state.get_order(up_wallet.wallet_id) if up_wallet else None
            down = state.get_order(down_wallet.wallet_id) if down_wallet else None
            up_filled = bool(up and up.status == OrderStatus.FILLED.value)
            down_filled = bool(down and down.status == OrderStatus.FILLED.value)
            if up_filled ^ down_filled:
                live_wallet = up_wallet if up_filled else down_wallet
                stale_wallet = down_wallet if up_filled else up_wallet
                stale_snapshot = down if up_filled else up
                live_snapshot = up if up_filled else down
                self._cancel_and_sell_stale(
                    state,
                    stale_wallet=stale_wallet,
                    live_wallet=live_wallet,
                    stale_snapshot=stale_snapshot,
                    live_entry=ExecutionOutcome(success=True, order_id=live_snapshot.order_id, price=live_snapshot.price, shares=live_snapshot.shares, raw={"source": "state"}),
                    clob_token_ids=clob_token_ids,
                    fee_rate_bps=fee_rate_bps,
                    condition_id=condition_id,
                )
                state.flow_state = EventFlowState.ENTRY_PARTIAL
                self._log_state(state, phase=state.flow_state.value, note="one side filled")
                return
            if up_filled and down_filled:
                state.first_fill_wallet_id = up_wallet.wallet_id if up_wallet else None
                state.second_fill_wallet_id = down_wallet.wallet_id if down_wallet else None
                state.flow_state = EventFlowState.ENTRY_CONFIRMED
                self._log_state(state, phase=state.flow_state.value, note="both sides filled")
                return
            time.sleep(self.poll_interval_sec)

        self._refresh_entry_order_statuses(state)
        if state.flow_state not in {EventFlowState.ENTRY_CONFIRMED, EventFlowState.HEDGE_CANCELLED, EventFlowState.FORCE_CLOSE_PLACED}:
            print(f"【等待】{state.event_name}：挂单后继续执行后续流程，当前未检测到显式成交回报，进入后续平仓/结算阶段")

    def _refresh_entry_order_statuses(self, state: DualWalletEventState) -> None:
        for wallet in self.wallets:
            snapshot = state.get_order(wallet.wallet_id)
            if not snapshot or snapshot.operation != OperationType.PLACE or snapshot.status != OrderStatus.SUBMITTED.value or not snapshot.order_id:
                continue
            outcome = self.executor.fetch_order_status(snapshot.order_id, wallet=wallet)
            if not outcome.success:
                continue
            raw = outcome.raw if isinstance(outcome.raw, dict) else {}
            status = str(raw.get("status") or "").lower()
            if status not in {OrderStatus.SUBMITTED.value, OrderStatus.FILLED.value, OrderStatus.CANCELLED.value, OrderStatus.FAILED.value}:
                continue
            snapshot.status = status
            if outcome.price is not None:
                snapshot.price = outcome.price
            if outcome.shares is not None:
                try:
                    snapshot.shares = float(outcome.shares)
                except (TypeError, ValueError):
                    pass
            state.mark_order(snapshot)

    def _force_close_if_needed(self, state: DualWalletEventState, *, clob_token_ids: list[str], fee_rate_bps: int, condition_id: str) -> None:
        if state.flow_state == EventFlowState.SETTLED:
            return
        self._force_close(state, clob_token_ids=clob_token_ids, fee_rate_bps=fee_rate_bps, condition_id=condition_id)

    def _cancel_and_sell_stale(self, state: DualWalletEventState, *, stale_wallet, live_wallet, stale_snapshot, live_entry, clob_token_ids, fee_rate_bps, condition_id) -> None:
        stale_side = state.side_by_wallet_id.get(stale_wallet.wallet_id) if stale_wallet else None
        live_side = state.side_by_wallet_id.get(live_wallet.wallet_id) if live_wallet else None
        stale_cancel = self.executor.cancel(stale_snapshot.order_id if stale_snapshot else None, wallet=stale_wallet)
        stale_cancel_snapshot = build_order_snapshot(wallet=stale_wallet, event_name=state.event_name, side=stale_side, amount_usd=stale_snapshot.amount_usd if stale_snapshot else 0.0, operation=OperationType.CANCEL, outcome=stale_cancel)
        stale_cancel_snapshot.status = OrderStatus.CANCELLED.value if stale_cancel.success else OrderStatus.FAILED.value
        state.mark_order(stale_cancel_snapshot)
        sell_amount = live_entry.shares * self.fixed_sell_price if live_entry.shares else 0.0
        sell = self.executor.place_entry(wallet=live_wallet, event_name=state.event_name, side=live_side, amount_usd=sell_amount, price=self.fixed_sell_price, clob_token_ids=clob_token_ids, fee_rate_bps=fee_rate_bps, condition_id=condition_id, order_type_override="GTC", post_only=False)
        sell_snapshot = build_order_snapshot(wallet=live_wallet, event_name=state.event_name, side=live_side, amount_usd=sell_amount, operation=OperationType.SELL, outcome=sell, close_price=self.fixed_sell_price)
        sell_snapshot.status = OrderStatus.FILLED.value if sell.success else OrderStatus.FAILED.value
        state.mark_order(sell_snapshot)
        state.flow_state = EventFlowState.HEDGE_CANCELLED
        state.first_fill_wallet_id = live_wallet.wallet_id
        self._log_state(state, phase=state.flow_state.value, note="stale leg cancelled and hedged")

    def _force_close(self, state: DualWalletEventState, *, clob_token_ids: list[str], fee_rate_bps: int, condition_id: str) -> None:
        for wallet in self.wallets:
            order = state.get_order(wallet.wallet_id)
            if order and order.order_id:
                self.executor.cancel(order.order_id, wallet=wallet)
                cancel_snapshot = build_order_snapshot(wallet=wallet, event_name=state.event_name, side=order.side, amount_usd=order.amount_usd, operation=OperationType.CANCEL, outcome=ExecutionOutcome(success=True, order_id=order.order_id, price=order.price, shares=order.shares, raw={"force_close": True}))
                cancel_snapshot.status = OrderStatus.CANCELLED.value
                state.mark_order(cancel_snapshot)
            close_amount = order.amount_usd if order else 0.0
            close = self.executor.place_entry(wallet=wallet, event_name=state.event_name, side=order.side if order else OrderSide.UP, amount_usd=close_amount, price=self.fixed_sell_price, clob_token_ids=clob_token_ids, fee_rate_bps=fee_rate_bps, condition_id=condition_id, order_type_override="FAK", post_only=False)
            close_snapshot = build_order_snapshot(wallet=wallet, event_name=state.event_name, side=order.side if order else OrderSide.UP, amount_usd=close_amount, operation=OperationType.FORCE_CLOSE, outcome=close, close_price=self.fixed_sell_price)
            close_snapshot.status = OrderStatus.FILLED.value if close.success else OrderStatus.FAILED.value
            state.mark_order(close_snapshot)
        state.flow_state = EventFlowState.FORCE_CLOSE_PLACED
        self._log_state(state, phase=state.flow_state.value, note="force close orders placed")

    def _build_summary(self, state: DualWalletEventState, *, condition_id: str) -> EventResultSummary:
        pnl_by_wallet: dict[str, float] = {wallet.wallet_id: 0.0 for wallet in self.wallets}
        filled = 0
        cancelled = 0
        force_closed = 0
        for snapshot in state.wallet_orders.values():
            if snapshot.status == OrderStatus.FILLED.value:
                filled += 1
            if snapshot.operation == OperationType.CANCEL and snapshot.status == OrderStatus.CANCELLED.value:
                cancelled += 1
            if snapshot.operation == OperationType.FORCE_CLOSE and snapshot.status == OrderStatus.FILLED.value:
                force_closed += 1
            if snapshot.price is None or snapshot.shares is None:
                continue
            if snapshot.operation == OperationType.PLACE and snapshot.status == OrderStatus.FILLED.value:
                pnl_by_wallet[snapshot.wallet.wallet_id] -= snapshot.amount_usd
            elif snapshot.operation == OperationType.SELL and snapshot.status == OrderStatus.FILLED.value and snapshot.close_price is not None:
                pnl_by_wallet[snapshot.wallet.wallet_id] += snapshot.shares * snapshot.close_price - snapshot.amount_usd
            elif snapshot.operation == OperationType.FORCE_CLOSE and snapshot.status == OrderStatus.FILLED.value and snapshot.close_price is not None:
                pnl_by_wallet[snapshot.wallet.wallet_id] += snapshot.shares * snapshot.close_price - snapshot.amount_usd

        outcome_payload = self._wait_for_market_outcome(condition_id=condition_id)
        outcome = self._parse_event_outcome(outcome_payload)
        wallet_balances = self._fetch_wallet_balances_after_settlement()
        total_pnl = sum(pnl_by_wallet.values())
        return EventResultSummary(
            event_name=state.event_name,
            outcome=outcome,
            total_pnl_usd=round(total_pnl, 6),
            wallet_pnl_usd={k: round(v, 6) for k, v in pnl_by_wallet.items()},
            wallet_balance_usdc=wallet_balances,
            order_count=len(state.wallet_orders),
            filled_count=filled,
            cancelled_count=cancelled,
            force_closed_count=force_closed,
            is_profit=total_pnl > 0,
            settled_at=datetime.now(timezone.utc),
        )

    def _wait_for_market_outcome(self, *, condition_id: str) -> dict[str, Any] | None:
        deadline = time.monotonic() + max(self.outcome_poll_timeout_sec, self.outcome_poll_interval_sec)
        last_payload: dict[str, Any] | None = None
        while True:
            payload = fetch_market_outcome(condition_id, slug=None, clob_token_ids=None)
            if isinstance(payload, dict):
                last_payload = payload
                if self._parse_event_outcome(payload) != EventOutcome.UNKNOWN:
                    return payload
            if time.monotonic() >= deadline:
                return last_payload
            time.sleep(max(1, self.outcome_poll_interval_sec))

    def _parse_event_outcome(self, outcome_payload: dict[str, Any] | None) -> EventOutcome:
        if isinstance(outcome_payload, dict):
            raw_outcome = str(outcome_payload.get("outcome") or outcome_payload.get("winner") or "").upper()
            if raw_outcome in {"UP", "YES"}:
                return EventOutcome.UP
            if raw_outcome in {"DOWN", "NO"}:
                return EventOutcome.DOWN
        return EventOutcome.UNKNOWN

    def _fetch_wallet_balances_after_settlement(self) -> dict[str, float | None]:
        balances: dict[str, float | None] = {}
        for wallet in self.wallets:
            payload = get_wallet_usdc_balance(account=wallet.account)
            balance = payload.get("balance_usdc") if isinstance(payload, dict) else None
            try:
                balances[wallet.wallet_id] = round(float(balance), 6) if balance is not None else None
            except (TypeError, ValueError):
                balances[wallet.wallet_id] = None
        return balances

    def _update_stop_guard(self, summary: EventResultSummary) -> None:
        self.loss_window.record(summary.is_profit)
        consecutive_losses = self.loss_window.consecutive_losses()
        if self.loss_window.should_halt(self.max_consecutive_losses):
            self._halted = True
            self._halt_reason = f"max_consecutive_losses_in_recent_window:{consecutive_losses}/5"
        else:
            self._halted = False
            self._halt_reason = None

    def _export_summary(self, state: DualWalletEventState, summary: EventResultSummary) -> None:
        consecutive_losses = self.loss_window.consecutive_losses()
        print(f"【事件结果】{state.event_name}：{'盈利' if summary.is_profit else '亏损'}；总收益={summary.total_pnl_usd:.4f}；最近窗口连续亏损={consecutive_losses}")
        print(f"【最终结果】{state.event_name}：{summary.outcome.value}")
        for wallet_id, pnl in summary.wallet_pnl_usd.items():
            balance = summary.wallet_balance_usdc.get(wallet_id)
            if balance is None:
                print(f"【钱包汇总】{wallet_id}：pnl={pnl:.4f} | available_usdc=UNKNOWN")
            else:
                print(f"【钱包汇总】{wallet_id}：pnl={pnl:.4f} | available_usdc={balance:.4f}")
        export_dual_wallet_event_to_excel(run_folder=self.run_folder, event_state=state, summary=summary, dry_run=self.dry_run)
        try:
            wallet_lines = []
            for wallet_id, pnl in summary.wallet_pnl_usd.items():
                balance = summary.wallet_balance_usdc.get(wallet_id)
                balance_text = "UNKNOWN" if balance is None else f"{balance:.4f}"
                wallet_lines.append(f"{wallet_id}: pnl={pnl:.4f}, available_usdc={balance_text}")
            send_feishu(
                title=f"双钱包事件结果 | {state.event_name}",
                content="\n".join([f"事件: {state.event_name}", f"最终结果: {summary.outcome.value}", f"总收益: {summary.total_pnl_usd:.4f}", f"是否盈利: {'是' if summary.is_profit else '否'}", f"最近窗口连续亏损: {self.loss_window.consecutive_losses()} / 5"] + wallet_lines),
                level="success" if summary.is_profit else "warn",
            )
        except Exception:
            pass
