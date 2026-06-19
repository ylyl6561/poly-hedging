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
        self.executor = DualWalletExecutor(
            dry_run=dry_run,
            dry_run_status_script=self.config.get("dual_wallet_dry_run_status_script", {}),
        )
        registry = get_account_registry()
        self.account_pool = AccountPool(registry.list_accounts())
        self.selected_accounts = self._select_two_dual_wallet_accounts()
        self.wallets = build_wallet_identities(self.selected_accounts)
        self.entry_timeout_sec = int(self.config.get("dual_wallet_entry_timeout_sec", 100))
        self.force_close_window_sec = int(self.config.get("dual_wallet_force_close_window_sec", 60))
        self.fixed_sell_price = float(self.config.get("dual_wallet_fixed_sell_price", 0.76))
        self.max_consecutive_losses = int(self.config.get("dual_wallet_max_consecutive_losses", 2))
        self.poll_interval_sec = int(self.config.get("dual_wallet_poll_interval_sec", 5))
        self.outcome_poll_interval_sec = int(self.config.get("dual_wallet_outcome_poll_interval_sec", 5))
        self.outcome_poll_timeout_sec = int(self.config.get("dual_wallet_outcome_poll_timeout_sec", 900))
        self.settlement_poll_interval_sec = int(self.config.get("dual_wallet_settlement_poll_interval_sec", 5))
        self.settlement_poll_timeout_sec = int(self.config.get("dual_wallet_settlement_poll_timeout_sec", 180))
        self.settlement_stable_rounds = int(self.config.get("dual_wallet_settlement_stable_rounds", 3))
        self.min_seconds_before_start = max(1, int(self.config.get("dual_wallet_min_seconds_before_start", 15)))
        self.loss_window = LossWindowTracker()
        self._halted = False
        self._halt_reason: str | None = None
        self._event_start_balances: dict[str, float | None] = {}

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

    def _seconds_until_start(self, start_time: datetime) -> float:
        return (start_time - datetime.now(timezone.utc)).total_seconds()

    @staticmethod
    def _normalize_event_times(start_time: datetime, end_time: datetime) -> tuple[datetime, datetime]:
        """Normalize event times to UTC-aware datetimes.

        This prevents timezone comparison bugs where naive datetimes are compared
        against UTC-aware datetimes, causing incorrect deadline calculations.

        Returns:
            Tuple of (start_time, end_time) both with timezone.utc
        """
        def to_utc_aware(dt: datetime) -> datetime:
            if dt.tzinfo is None:
                # Naive datetime: assume it's in local timezone and convert to UTC
                return dt.astimezone(timezone.utc)
            return dt.astimezone(timezone.utc)

        return to_utc_aware(start_time), to_utc_aware(end_time)

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
        # Normalize event times to UTC-aware datetimes to prevent timezone comparison bugs
        start_time, end_time = self._normalize_event_times(start_time, end_time)
        seconds_until_start = self._seconds_until_start(start_time)
        if seconds_until_start < self.min_seconds_before_start:
            print(
                f"【跳过】{event_name}：距离开始仅剩 {seconds_until_start:.1f}s，"
                f"小于最小提前量 {self.min_seconds_before_start}s，不执行挂单"
            )
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
        self._snapshot_start_balances()
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

    def _log_state(self, state: DualWalletEventState, *, phase: str, note: str | None = None, payload: dict[str, Any] | None = None) -> None:
        if not self.structured_log:
            return
        extra_payload = payload or {}
        self.structured_log.record_event_state(
            event_name=state.event_name,
            event_id=state.event_id,
            flow_state=phase,
            wallet_status={wallet_id: snapshot.status for wallet_id, snapshot in state.wallet_orders.items()},
            note=note,
            payload={
                "selected_accounts": self._build_selected_account_payload(),
                "side_assignment": self._build_side_assignment_payload(state),
                **extra_payload,
            },
        )

    def _announce_event(self, state: DualWalletEventState, *, condition_id: str, amount_usd: float, up_price: float, down_price: float) -> None:
        print(f"【事件】{state.event_name}")
        print(f"【窗口】{state.start_time.astimezone(timezone.utc).isoformat()} UTC -> {state.end_time.astimezone(timezone.utc).isoformat()} UTC")
        print(f"【市场】condition_id={condition_id} | close_price={self.fixed_sell_price:.4f} | timeout={self.entry_timeout_sec}s")
        print(f"【下单门槛】距开始至少 {self.min_seconds_before_start}s | 当前剩余 {self._seconds_until_start(state.start_time):.1f}s")
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

    def _snapshot_start_balances(self) -> None:
        self._event_start_balances = {}
        account_data = self._fetch_wallet_account_data()
        for wallet in self.wallets:
            payload = account_data.get(wallet.wallet_id, {})
            balance = payload.get("balance_usdc") if isinstance(payload, dict) else None
            try:
                self._event_start_balances[wallet.wallet_id] = round(float(balance), 6) if balance is not None else None
            except (TypeError, ValueError):
                self._event_start_balances[wallet.wallet_id] = None

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
        error_text = getattr(snapshot, "error", None) or ""
        error_detail = f" | error={error_text}" if error_text else ""
        print(
            f"【挂单】{wallet.wallet_name}({wallet.account.account_id})"
            f" | side={side_text}"
            f" | amount_usd={float(amount_usd):.4f}"
            f" | price={price_text}"
            f" | shares={shares_text}"
            f" | status={status_text}{error_detail}"
            f" | token_id={token_id_text}"
            f" | condition_id={condition_id_text}"
            f" | order_id={order_id_text}"
        )

    def _wait_and_handle_partials(self, state: DualWalletEventState, *, clob_token_ids: list[str], fee_rate_bps: int, condition_id: str) -> None:
        close_window_sec = max(1, int(self.force_close_window_sec))
        now = datetime.now(timezone.utc)

        # Normalize end_time to UTC-aware
        end_time_utc = state.end_time
        if end_time_utc.tzinfo is None:
            print(f"【警告】{state.event_name}：end_time 缺少时区信息，假设为本地时间并转换到 UTC")
            end_time_utc = end_time_utc.astimezone(timezone.utc)
            state.end_time = end_time_utc

        # Normalize start_time to UTC-aware
        start_time_utc = state.start_time
        if start_time_utc.tzinfo is None:
            print(f"【警告】{state.event_name}：start_time 缺少时区信息，假设为本地时间并转换到 UTC")
            start_time_utc = start_time_utc.astimezone(timezone.utc)
            state.start_time = start_time_utc

        time_to_event_start = (start_time_utc - now).total_seconds()
        time_to_event_end = (end_time_utc - now).total_seconds()

        # Phase 1: 事件已结束 → 跳过等待，直接结算
        if time_to_event_end <= 0:
            print(f"【等待成交】{state.event_name}：事件已结束（当前 {now.strftime('%H:%M:%S')} UTC，距结束 {time_to_event_end:.1f}s），跳过等待成交阶段")
            state.trigger_reason = "event_already_ended"
            state.trigger_detail = f"remaining_to_end_sec={int(time_to_event_end)};event_ended_before_wait"
            state.flow_state = state.flow_state  # preserve existing flow_state
            self._log_state(state, phase=state.flow_state.value, note=f"事件已结束，跳过等待阶段")
            return

        # Phase 2: 事件未开始 → 短轮询等到事件开始（避免长时间阻塞，且能在等待中响应提前成交）
        if time_to_event_start > 0:
            print(f"【等待成交】{state.event_name}：事件尚未开始（距开始 {time_to_event_start:.1f}s），进入短轮询等待直到事件开始...")
            sleep_start = time.time()
            # 短轮询 sleep，每 1 秒醒一次重新计算剩余时间
            pre_start_poll_sec = 1.0
            while True:
                now = datetime.now(timezone.utc)
                remaining_to_start = (start_time_utc - now).total_seconds()
                if remaining_to_start <= 0:
                    break
                # 单次 sleep 不超过 pre_start_poll_sec
                time.sleep(min(pre_start_poll_sec, remaining_to_start))
            waited_sec = time.time() - sleep_start
            now = datetime.now(timezone.utc)
            time_to_event_start = (start_time_utc - now).total_seconds()
            time_to_event_end = (end_time_utc - now).total_seconds()
            print(f"【等待成交】{state.event_name}：等待完成（等了 {waited_sec:.1f}s），当前 {now.strftime('%H:%M:%S')} UTC | 距开始 {time_to_event_start:.1f}s | 距结束 {time_to_event_end:.1f}s")

            # 等到事件开始后，再次检查是否已经结束（理论上不可能，但防御性检查）
            if time_to_event_end <= 0:
                print(f"【等待成交】{state.event_name}：事件在等待期间已结束，跳过等待成交阶段")
                state.trigger_reason = "event_already_ended"
                state.trigger_detail = f"remaining_to_end_sec={int(time_to_event_end)};event_ended_after_pre_start_wait"
                self._log_state(state, phase=state.flow_state.value, note=f"等待期间事件结束，跳过阶段")
                return

        # Phase 3: 事件已开始，正常等待成交期
        # entry_timeout 从事件开始时间 (start_time_utc) 起算，强平窗口从 end_time 倒推。
        # 单边成交处理必须等到 entry_timeout 期满后才执行（与"单边成交等待超时时间（秒）"配置语义一致），
        # 给另一侧同样的等待窗口，避免 UP 30s 成交、60s 撤销 DOWN 时 DOWN 还在 100ms 内即将成交的过早撤单。
        # 强平窗口（close_window）则只用于外层 break 退出，与单边成交处理逻辑解耦。
        deadline_by_timeout = start_time_utc + timedelta(seconds=self.entry_timeout_sec)
        deadline_by_close_window = end_time_utc - timedelta(seconds=close_window_sec)
        # 主等待 deadline = entry_timeout（不再被 close_window 截断；close_window 用作循环内的额外 break 条件）
        deadline = deadline_by_timeout
        # 事件开始后到 deadline 的剩余时间，即"实际还能等待多久"
        effective_wait_sec = (deadline - now).total_seconds()
        timeout_capped = False
        print(f"【等待成交】{state.event_name}：开始等待成交 | 距开始 {time_to_event_start:.1f}s | 距结束 {time_to_event_end:.1f}s | 强平窗口 {close_window_sec}s")
        print(f"【等待成交】{state.event_name}：deadline计算 | entry_timeout={self.entry_timeout_sec}s（自事件开始 {start_time_utc.strftime('%H:%M:%S')} UTC 起算） | 强平窗口前（{close_window_sec}s前）→ {time_to_event_end - close_window_sec:.1f}s后到期 | 单边处理等待至 entry_timeout 满 | 实际剩余等待 {effective_wait_sec:.1f}s | {'⚠️ 被强平窗口截断' if timeout_capped else '✅ 正常'}")
        while datetime.now(timezone.utc) < deadline:
            # 强平窗口也已到达 → 跳出循环交给外层走强平路径（不再等 entry_timeout）
            if datetime.now(timezone.utc) >= deadline_by_close_window:
                break
            self._refresh_entry_order_statuses(state)
            up_wallet = next((wallet for wallet in self.wallets if state.side_by_wallet_id.get(wallet.wallet_id) == OrderSide.UP), None)
            down_wallet = next((wallet for wallet in self.wallets if state.side_by_wallet_id.get(wallet.wallet_id) == OrderSide.DOWN), None)
            up = state.get_order(up_wallet.wallet_id) if up_wallet else None
            down = state.get_order(down_wallet.wallet_id) if down_wallet else None
            up_filled = bool(up and up.status == OrderStatus.FILLED.value and (up.filled_shares or up.shares))
            down_filled = bool(down and down.status == OrderStatus.FILLED.value and (down.filled_shares or down.shares))
            if up_filled and down_filled:
                # 双边已成交：保留现有行为，立即确认 + return
                state.first_fill_wallet_id = up_wallet.wallet_id if up_wallet else None
                state.second_fill_wallet_id = down_wallet.wallet_id if down_wallet else None
                up_account = f"{up_wallet.wallet_name}({up_wallet.account.account_id})" if up_wallet else "UNKNOWN"
                down_account = f"{down_wallet.wallet_name}({down_wallet.account.account_id})" if down_wallet else "UNKNOWN"
                up_filled_shares = float(up.filled_shares or up.shares or 0.0) if up else 0.0
                down_filled_shares = float(down.filled_shares or down.shares or 0.0) if down else 0.0
                print(
                    f"【双边成交确认】{state.event_name}："
                    f"UP账号={up_account} | UP成交份额={up_filled_shares:.4f} | "
                    f"DOWN账号={down_account} | DOWN成交份额={down_filled_shares:.4f}"
                )
                state.trigger_reason = "both_sides_filled"
                state.trigger_detail = f"up_account={up_account};down_account={down_account};up_shares={up_filled_shares:.4f};down_shares={down_filled_shares:.4f}"
                state.flow_state = EventFlowState.ENTRY_CONFIRMED
                self._log_state(state, phase=state.flow_state.value, note=f"双边成交确认：UP账号={up_account}，DOWN账号={down_account}")
                return
            # 单边成交：entry_timeout 未到期不撤单，继续等另一侧同样的窗口（避免过早撤单）
            # 仅在 entry_timeout 已过（循环结束时 final check）才执行"单边成交处理"
            time.sleep(self.poll_interval_sec)

        # 循环结束：最后一次 refresh + 一次最终单边成交判断
        self._refresh_entry_order_statuses(state)
        up_wallet = next((wallet for wallet in self.wallets if state.side_by_wallet_id.get(wallet.wallet_id) == OrderSide.UP), None)
        down_wallet = next((wallet for wallet in self.wallets if state.side_by_wallet_id.get(wallet.wallet_id) == OrderSide.DOWN), None)
        up = state.get_order(up_wallet.wallet_id) if up_wallet else None
        down = state.get_order(down_wallet.wallet_id) if down_wallet else None
        up_filled = bool(up and up.status == OrderStatus.FILLED.value and (up.filled_shares or up.shares))
        down_filled = bool(down and down.status == OrderStatus.FILLED.value and (down.filled_shares or down.shares))
        if up_filled ^ down_filled and up and down:
            # entry_timeout 之后仍是单边成交 → 走单边成交处理（撤另一侧 + 挂 GTC 抛售单）
            live_wallet = up_wallet if up_filled else down_wallet
            stale_wallet = down_wallet if up_filled else up_wallet
            stale_snapshot = down if up_filled else up
            live_snapshot = up if up_filled else down
            self._cancel_and_sell_stale(
                state,
                stale_wallet=stale_wallet,
                live_wallet=live_wallet,
                stale_snapshot=stale_snapshot,
                live_entry=ExecutionOutcome(
                    success=True,
                    order_id=live_snapshot.order_id,
                    price=live_snapshot.average_fill_price or live_snapshot.price,
                    shares=live_snapshot.filled_shares or live_snapshot.shares,
                    filled_amount_usd=live_snapshot.filled_amount_usd,
                    filled_shares=live_snapshot.filled_shares or live_snapshot.shares,
                    average_fill_price=live_snapshot.average_fill_price or live_snapshot.price,
                    raw={"source": "state"},
                ),
                clob_token_ids=clob_token_ids,
                fee_rate_bps=fee_rate_bps,
                condition_id=condition_id,
            )
            filled_side = "UP" if up_filled else "DOWN"
            live_account = f"{live_wallet.wallet_name}({live_wallet.account.account_id})" if live_wallet else "UNKNOWN"
            stale_account = f"{stale_wallet.wallet_name}({stale_wallet.account.account_id})" if stale_wallet else "UNKNOWN"
            live_filled_shares = float(live_snapshot.filled_shares or live_snapshot.shares or 0.0) if live_snapshot else 0.0
            cancel_snapshot_recent = next(
                (s for s in reversed(state.get_order_history(stale_wallet.wallet_id)) if s.operation == OperationType.CANCEL),
                None,
            )
            cancel_ok = bool(cancel_snapshot_recent and cancel_snapshot_recent.status == OrderStatus.CANCELLED.value)
            cancel_err = (cancel_snapshot_recent.error if cancel_snapshot_recent else "") or ""
            stale_text = (
                f"已撤另一侧挂单（{stale_account}）"
                if cancel_ok
                else (
                    f"撤另一侧挂单失败（{stale_account}, error={cancel_err or 'unknown'}）—单边成交下保留份额仍由强平窗口兜底"
                )
            )
            print(
                f"【单边成交处理】{state.event_name}：仅 {filled_side} 侧成交"
                f" | 成交账号={live_account}"
                f" | {stale_text}"
                f" | 成交份额={live_filled_shares:.4f}"
                f" | 保留份额等待强平窗口（事件结束前 ≤ {int(self.force_close_window_sec)}s）卖出"
            )
            self._log_state(
                state,
                phase=state.flow_state.value,
                note=f"单边成交处理：成交账号={live_account}={live_filled_shares:.4f}份额，撤单账号={stale_account}，延后到强平窗口卖出"
            )
            # 走完单边处理后直接 return，外层 _force_close_if_needed 由事件结束前的强平窗口触发
            return

        self._refresh_entry_order_statuses(state)
        remaining_to_end = state.remaining_to_end(datetime.now(timezone.utc))
        now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"【等待成交】{state.event_name}：循环结束 | 当前 {now_str} UTC | 距事件结束 {remaining_to_end:.1f}s | 强平窗口阈值 {close_window_sec}s")
        # Do not overwrite a single_side_fill_pending_close trigger — it carries the抛售单 metadata
        # (sell_order_submitted) that _force_close uses to decide whether to FAK or skip.
        preserve_reason = state.trigger_reason in {"single_side_fill_pending_close", "force_close_window"}
        if remaining_to_end <= 0:
            print(f"【跳过强平】{state.event_name}：事件已结束（距结束 {remaining_to_end:.1f}s），不再尝试平仓，直接进入结算流程")
            if not preserve_reason:
                state.trigger_reason = "event_already_ended"
                state.trigger_detail = f"remaining_to_end_sec={int(remaining_to_end)};event_ended_before_force_close"
            self._log_state(state, phase=state.flow_state.value, note=f"事件已结束，跳过强平窗口")
        elif remaining_to_end <= close_window_sec:
            print(f"【{close_window_sec}秒窗口强平】{state.event_name}：距离事件结束仅剩 {max(0, int(remaining_to_end))} 秒，停止等待初始挂单，进入后续平仓/结算阶段")
            if not preserve_reason:
                state.trigger_reason = "force_close_window"
                state.trigger_detail = f"remaining_to_end_sec={max(0, int(remaining_to_end))};close_window_sec={close_window_sec}"
            self._log_state(state, phase=state.flow_state.value, note=f"进入{close_window_sec}秒强平窗口")
        elif state.flow_state not in {EventFlowState.ENTRY_CONFIRMED, EventFlowState.HEDGE_CANCELLED, EventFlowState.FORCE_CLOSE_PLACED}:
            print(f"【等待成交】{state.event_name}：等待阶段结束（entry_timeout到期，{effective_wait_sec:.1f}s内未检测到明确双边成交），转入后续平仓/结算流程")
            if not preserve_reason:
                state.trigger_reason = "wait_timeout_no_clear_fill"
                state.trigger_detail = f"entry_timeout_sec={self.entry_timeout_sec};effective_wait_sec={effective_wait_sec:.1f};remaining_to_end_sec={int(remaining_to_end)}"

    def _refresh_open_order_statuses(self, state: DualWalletEventState) -> None:
        """Refresh the CLOB status of any SUBMITTED order snapshot.

        Covers both initial PLACE (GTC buy) and the GTC 抛售单 (SELL) that we
        may have placed on a single-side fill. We need both kinds of snapshots
        refreshed so that a抛售单 matched by the market during
        entry_timeout→close_window is correctly seen as FILLED before
        _force_close decides whether to issue a FAK 平仓.
        """
        for wallet in self.wallets:
            snapshot = state.get_order(wallet.wallet_id)
            if (
                not snapshot
                or not snapshot.order_id
                or snapshot.status != OrderStatus.SUBMITTED.value
            ):
                continue
            # Place entry GTC buy and the GTC 抛售单 (SELL) both live on the order book as SUBMITTED.
            # We must refresh both, because the GTC 抛售单 may have been matched by the market in
            # the entry_timeout→close_window window, and a filled抛售单 means the position is
            # already flat — _force_close must skip the FAK 平仓 on that side.
            if snapshot.operation not in {OperationType.PLACE, OperationType.SELL}:
                continue
            outcome = self.executor.fetch_order_status(snapshot.order_id, wallet=wallet)
            if not outcome.success:
                continue
            raw = outcome.raw if isinstance(outcome.raw, dict) else {}
            status = str(raw.get("status") or "").lower()
            if status not in {OrderStatus.SUBMITTED.value, OrderStatus.FILLED.value, OrderStatus.CANCELLED.value, OrderStatus.FAILED.value}:
                continue
            snapshot.status = status
            snapshot.raw_status = outcome.raw_status or raw.get("raw_status") or raw.get("status")
            if outcome.price is not None:
                snapshot.price = outcome.price
            if outcome.shares is not None:
                try:
                    snapshot.shares = float(outcome.shares)
                except (TypeError, ValueError):
                    pass
            if outcome.filled_shares is not None:
                try:
                    snapshot.filled_shares = float(outcome.filled_shares)
                except (TypeError, ValueError):
                    pass
            if outcome.filled_amount_usd is not None:
                try:
                    snapshot.filled_amount_usd = float(outcome.filled_amount_usd)
                except (TypeError, ValueError):
                    pass
            if outcome.average_fill_price is not None:
                try:
                    snapshot.average_fill_price = float(outcome.average_fill_price)
                except (TypeError, ValueError):
                    pass
            state.mark_order(snapshot)
            if self.structured_log:
                self.structured_log.record_order(snapshot)

    def _refresh_entry_order_statuses(self, state: DualWalletEventState) -> None:
        """Backwards-compatible alias for _refresh_open_order_statuses.

        The old name only covered the initial PLACE snapshot, but we now also
        need to refresh the GTC 抛售单 (SELL) snapshot before _force_close runs.
        """
        self._refresh_open_order_statuses(state)

    def _force_close_if_needed(self, state: DualWalletEventState, *, clob_token_ids: list[str], fee_rate_bps: int, condition_id: str) -> None:
        if state.flow_state == EventFlowState.SETTLED:
            return
        # Only force close when a force-close trigger exists (set by either _wait_and_handle_partials
        # or _cancel_and_sell_stale), and we are within (or past) the configured close window.
        if state.trigger_reason not in {"force_close_window", "single_side_fill_pending_close"}:
            return
        close_window_sec = max(1, int(self.force_close_window_sec))
        # Time gate: strong-flat only fires within force_close_window_sec of event end.
        # This enforces the contract "60秒窗口强平" — the close window is counted back from end_time.
        now = datetime.now(timezone.utc)
        end_time_utc = state.end_time
        if end_time_utc.tzinfo is None:
            end_time_utc = end_time_utc.astimezone(timezone.utc)
        remaining_to_end = (end_time_utc - now).total_seconds()
        if remaining_to_end > close_window_sec:
            wait_start = time.time()
            print(
                f"【{close_window_sec}秒窗口强平】{state.event_name}：尚未进入强平窗口"
                f" | 距事件结束 {remaining_to_end:.1f}s > 窗口 {close_window_sec}s"
                f" | 触发原因={state.trigger_reason}"
                f" | 进入短轮询等待直到进入窗口..."
            )
            poll_sec = 1.0
            while True:
                now = datetime.now(timezone.utc)
                remaining_to_end = (end_time_utc - now).total_seconds()
                if remaining_to_end <= close_window_sec:
                    break
                time.sleep(min(poll_sec, max(0.0, remaining_to_end - close_window_sec)))
            waited = time.time() - wait_start
            print(
                f"【{close_window_sec}秒窗口强平】{state.event_name}：已进入强平窗口"
                f"（等了 {waited:.1f}s）| 距事件结束 {remaining_to_end:.1f}s ≤ 窗口 {close_window_sec}s"
                f" | 触发原因={state.trigger_reason} | 开始执行撤单+平仓"
            )
        self._force_close(state, clob_token_ids=clob_token_ids, fee_rate_bps=fee_rate_bps, condition_id=condition_id)

    def _cancel_and_sell_stale(self, state: DualWalletEventState, *, stale_wallet, live_wallet, stale_snapshot, live_entry, clob_token_ids, fee_rate_bps, condition_id) -> tuple[ExecutionOutcome, ExecutionOutcome]:
        """
        单边成交处理：撤另一侧挂单 + 对已成交侧挂 GTC 抛售单（不立即 FAK 强平）。

        语义：
          - 撤 stale 侧（避免对冲失败后长期挂单）
          - live 侧按 fixed_sell_price 挂 GTC 卖单，让市场在 entry_timeout→end-close_window
            区间内慢慢吃掉；price=fixed_sell_price（默认 0.6）是"中性对冲价"，单边成交时
            锁定约 (fixed_sell_price - entry_price) 的 PnL。
          - 只有到了强平窗口（now >= end - force_close_window_sec）_force_close_if_needed 才会
            真正以 FAK 强平；如果抛售单已经被市场吃掉则 _force_close 跳过该侧。

        返回 (cancel_outcome, sell_outcome)，调用方可据此打印真实结果。
        """
        stale_side = state.side_by_wallet_id.get(stale_wallet.wallet_id) if stale_wallet else None
        live_side = state.side_by_wallet_id.get(live_wallet.wallet_id) if live_wallet else None
        # 1) 撤另一侧
        stale_cancel = self.executor.cancel(stale_snapshot.order_id if stale_snapshot else None, wallet=stale_wallet)
        # 1a) 撤单失败时回查真实状态——订单可能其实已经成交（撤单API对已成交单报 'str object has no attribute orderID'
        #     这类错误是历史bug，但即便修好cancel，订单也可能在cancel被发出前被市场吃掉了）。把"事实上已成交"视为
        #     撤单成功：单边成交的两侧现在都已是 FILLED，state自然走到双边成交确认。
        if not stale_cancel.success and stale_snapshot and stale_snapshot.order_id:
            refresh = self.executor.fetch_order_status(stale_snapshot.order_id, wallet=stale_wallet)
            if refresh.success and isinstance(refresh.raw, dict):
                latest_status = str(refresh.raw.get("status") or "").lower()
                if latest_status in {"matched", "filled", "executed"}:
                    stale_cancel_snapshot = build_order_snapshot(
                        wallet=stale_wallet, event_name=state.event_name, side=stale_side,
                        amount_usd=stale_snapshot.amount_usd if stale_snapshot else 0.0,
                        operation=OperationType.CANCEL, outcome=stale_cancel,
                    )
                    # 标记为 CANCELLED 表示该单已不在簿上（事实上的终态）
                    stale_cancel_snapshot.status = OrderStatus.CANCELLED.value
                    stale_cancel_snapshot.note = f"cancel_api_failed_but_order_terminal_filled:{latest_status}"
                    state.mark_order(stale_cancel_snapshot)
                    # 同步刷新 stale_snapshot 的状态为 FILLED，使后续轮询走"双边成交"路径
                    stale_snapshot.status = OrderStatus.FILLED.value
                    stale_snapshot.raw_status = latest_status
                    if refresh.filled_shares is not None:
                        try:
                            stale_snapshot.filled_shares = float(refresh.filled_shares)
                        except (TypeError, ValueError):
                            pass
                    if refresh.average_fill_price is not None:
                        try:
                            stale_snapshot.average_fill_price = float(refresh.average_fill_price)
                        except (TypeError, ValueError):
                            pass
                    state.mark_order(stale_snapshot)
                    print(
                        f"【单边成交处理-撤单回查】{state.event_name}："
                        f"撤单API失败但回查发现 stale 侧实际已成交（status={latest_status}），"
                        f"按已不在簿处理，stale 侧也标记 FILLED"
                    )
                    # 调整 stale_cancel.success 以走 print 分支（成功）
                    stale_cancel = ExecutionOutcome(
                        success=True, order_id=stale_snapshot.order_id,
                        note=f"benign_terminal_via_refresh:{latest_status}", raw=refresh.raw,
                    )
        stale_cancel_snapshot = build_order_snapshot(wallet=stale_wallet, event_name=state.event_name, side=stale_side, amount_usd=stale_snapshot.amount_usd if stale_snapshot else 0.0, operation=OperationType.CANCEL, outcome=stale_cancel)
        stale_cancel_snapshot.status = OrderStatus.CANCELLED.value if stale_cancel.success else OrderStatus.FAILED.value
        state.mark_order(stale_cancel_snapshot)
        stale_account = f"{stale_wallet.wallet_name}({stale_wallet.account.account_id})" if stale_wallet else "UNKNOWN"
        live_account = f"{live_wallet.wallet_name}({live_wallet.account.account_id})" if live_wallet else "UNKNOWN"
        # 2) 挂 GTC 抛售单（GTC 让市场在 entry_timeout→close_window 区间吃；FAK 只在 close_window 才用）
        live_filled_shares = float(live_entry.filled_shares or live_entry.shares or 0.0) if live_entry else 0.0
        # 抛售份额 = 已成交的 token 数（不是 USD）。如果 entry 阶段没有填到具体 token 数，
        # 用 USD notional / fixed_sell_price 推算 token 数。amount_usd 仅用于构建 OrderSnapshot。
        sell_shares = live_filled_shares
        if sell_shares <= 0:
            fallback_usd = live_entry.filled_amount_usd or 0.0
            if fallback_usd and self.fixed_sell_price:
                sell_shares = float(fallback_usd) / float(self.fixed_sell_price)
        sell_amount_usd = sell_shares * float(self.fixed_sell_price) if self.fixed_sell_price else 0.0
        sell = self.executor.place_sell(
            wallet=live_wallet,
            event_name=state.event_name,
            side=live_side,
            shares=sell_shares,
            price=self.fixed_sell_price,
            clob_token_ids=clob_token_ids,
            fee_rate_bps=fee_rate_bps,
            condition_id=condition_id,
            order_type_override="GTC",
            post_only=False,
        )
        sell_snapshot = build_order_snapshot(
            wallet=live_wallet,
            event_name=state.event_name,
            side=live_side,
            amount_usd=sell_amount_usd,
            operation=OperationType.SELL,  # 抛售单（在事件结束前留挂在订单簿上供市场吃）
            outcome=sell,
            close_price=self.fixed_sell_price,
        )
        sell_snapshot.status = OrderStatus.SUBMITTED.value if sell.success else OrderStatus.FAILED.value
        state.mark_order(sell_snapshot)
        sell_result = "成功" if sell.success else "失败"
        cancel_result = "成功" if stale_cancel.success else "失败"
        cancel_error_detail = "" if stale_cancel.success else f" | error={stale_cancel.error or 'unknown'}"
        sell_error_detail = "" if sell.success else f" | error={sell.error or 'unknown'}"
        sell_order_id_part = f" | 抛售单order_id={sell.order_id}" if sell.order_id else ""
        cancel_order_id_part = f" | 撤单order_id={stale_snapshot.order_id}" if stale_snapshot and stale_snapshot.order_id else ""
        print(
            f"【单边成交处理-账号动作】{state.event_name}："
            f"撤单账号={stale_account} | 撤单结果={cancel_result}{cancel_error_detail}{cancel_order_id_part} | "
            f"抛售账号={live_account} | 抛售价格={self.fixed_sell_price:.4f} | 抛售份额={sell_shares:.4f} | 抛售结果={sell_result}{sell_error_detail}{sell_order_id_part} | "
            f"动作=仅撤单+挂 GTC 抛售单，强平（FAK）延后到 end_time-{int(self.force_close_window_sec)}s 窗口；届时若抛售单已成交则跳过强平"
        )
        state.flow_state = EventFlowState.HEDGE_CANCELLED
        state.first_fill_wallet_id = live_wallet.wallet_id
        state.trigger_reason = "single_side_fill_pending_close"
        state.trigger_detail = (
            f"filled_side={'UP' if live_wallet and state.side_by_wallet_id.get(live_wallet.wallet_id) == OrderSide.UP else 'DOWN'};"
            f"live_account={live_account};stale_account={stale_account};"
            f"filled_shares={live_filled_shares:.4f};sell_order_submitted={sell.success};"
            f"cancel_success={stale_cancel.success};cancel_error={stale_cancel.error or ''};"
            f"awaiting_force_close_window"
        )
        self._log_state(
            state,
            phase=state.flow_state.value,
            note=(
                f"单边成交处理：撤单账号={stale_account}={cancel_result}({stale_cancel.error or 'n/a'})，"
                f"抛售账号={live_account} 挂 GTC @ {self.fixed_sell_price:.4f} "
                f"({live_filled_shares:.4f}份额)={sell_result}({sell.error or 'n/a'})；"
                f"FAK 强平延后到强平窗口（end-{int(self.force_close_window_sec)}s）"
            ),
        )
        return stale_cancel, sell

    def _force_close(self, state: DualWalletEventState, *, clob_token_ids: list[str], fee_rate_bps: int, condition_id: str) -> None:
        self._refresh_open_order_statuses(state)
        close_window_sec = max(1, int(self.force_close_window_sec))
        selected_accounts = [f"{wallet.wallet_name}({wallet.account.account_id})" for wallet in self.wallets]
        is_single_side_pending = state.trigger_reason == "single_side_fill_pending_close"

        # For single_side_fill_pending_close: recover original entry filled_shares from history
        # since the current order snapshot may be the GTC 抛售单 (filled_shares == 0 if not yet matched).
        entry_filled_by_wallet: dict[str, float] = {}
        if is_single_side_pending and state.first_fill_wallet_id:
            for wid, history in state.wallet_order_history.items():
                for snap in history:
                    if snap.operation == OperationType.PLACE and snap.status == OrderStatus.FILLED.value:
                        entry_filled_by_wallet[wid] = float(snap.filled_shares or snap.shares or 0.0)
                        break

        # If the GTC 抛售单 was matched by the market in entry_timeout→close_window, the position
        # is already flat. We must NOT issue a FAK 平仓 on that side — the wallet has no
        # tokens left to sell (they were already transferred by the filled抛售单), and a redundant
        # FAK will fail with "not enough balance" and obscure the real PnL.
        # This dict is the source of truth and is populated by _refresh_open_order_statuses
        # above, which now also refreshes SELL snapshots.
        sell_order_filled_by_wallet: dict[str, bool] = {}
        for wallet in self.wallets:
            history = state.get_order_history(wallet.wallet_id)
            sell_order_filled_by_wallet[wallet.wallet_id] = any(
                snap.operation == OperationType.SELL and snap.status == OrderStatus.FILLED.value
                for snap in history
            )

        print(
            f"【{close_window_sec}秒窗口强平】{state.event_name}：开始执行强平"
            f" | 作用账号={' | '.join(selected_accounts)}"
            f" | 触发原因={state.trigger_reason}"
            f" | 处理顺序=逐账号：撤未成交挂单 → 校验撤单结果 → 已成交且抛售单未吃 → FAK 平仓；"
            f"抛售单已成交则跳过 FAK"
        )
        for wallet in self.wallets:
            order = state.get_order(wallet.wallet_id)
            account_text = f"{wallet.wallet_name}({wallet.account.account_id})"

            # 1) If the GTC 抛售单 (or any current order) is already FILLED, the position is flat —
            # skip cancel + FAK for this wallet. Cancel a filled order would be a no-op at best
            # and a waste of an API call at worst.
            if is_single_side_pending and sell_order_filled_by_wallet.get(wallet.wallet_id, False):
                print(
                    f"【{close_window_sec}秒窗口强平-平仓】{state.event_name}：账号={account_text} | "
                    f"抛售单已被市场吃掉（GTC SELL filled），跳过撤单 + FAK 强平"
                )
                continue

            # 2) Try to cancel any SUBMITTED order on the book. For single_side_fill_pending_close
            # the current snapshot is the GTC 抛售单; for the un-filled side it is the original PLACE.
            cancel_already_terminal = False
            if order and order.order_id and order.status == OrderStatus.SUBMITTED.value:
                cancel_outcome = self.executor.cancel(order.order_id, wallet=wallet)
                cancel_snapshot = build_order_snapshot(
                    wallet=wallet, event_name=state.event_name, side=order.side,
                    amount_usd=order.amount_usd, operation=OperationType.CANCEL, outcome=cancel_outcome,
                )
                cancel_snapshot.status = OrderStatus.CANCELLED.value if cancel_outcome.success else OrderStatus.FAILED.value
                state.mark_order(cancel_snapshot)
                cancel_error_detail = "" if cancel_outcome.success else f" | error={cancel_outcome.error or 'unknown'}"
                note_detail = ""
                if cancel_outcome.note:
                    note_detail = f" | note={cancel_outcome.note}"
                cancel_outcome_text = "成功" if cancel_outcome.success else "失败"
                print(
                    f"【{close_window_sec}秒窗口强平-撤单】{state.event_name}："
                    f"账号={account_text} | side={order.side.value if order and order.side else 'UNKNOWN'}"
                    f" | order_id={order.order_id} | 结果={cancel_outcome_text}{cancel_error_detail}{note_detail}"
                )
                if self.structured_log:
                    self.structured_log.record_order(cancel_snapshot)
                # 2a) Re-fetch status to be sure. cancel_order may have been a no-op because the
                # order was matched between our last refresh and this cancel attempt. If it is
                # now FILLED, treat it as 抛售单 already filled and skip the FAK below.
                refresh = self.executor.fetch_order_status(order.order_id, wallet=wallet)
                if refresh.success and isinstance(refresh.raw, dict):
                    latest_status = str(refresh.raw.get("status") or "").lower()
                    normalized = {
                        "live": OrderStatus.SUBMITTED.value, "open": OrderStatus.SUBMITTED.value,
                        "pending": OrderStatus.SUBMITTED.value,
                        "matched": OrderStatus.FILLED.value, "filled": OrderStatus.FILLED.value,
                        "executed": OrderStatus.FILLED.value,
                        "cancelled": OrderStatus.CANCELLED.value, "canceled": OrderStatus.CANCELLED.value,
                        "failed": OrderStatus.FAILED.value, "rejected": OrderStatus.FAILED.value,
                    }.get(latest_status, latest_status)
                    if normalized == OrderStatus.FILLED.value:
                        order.status = OrderStatus.FILLED.value
                        order.raw_status = latest_status
                        if refresh.filled_shares is not None:
                            try:
                                order.filled_shares = float(refresh.filled_shares)
                            except (TypeError, ValueError):
                                pass
                        state.mark_order(order)
                        print(
                            f"【{close_window_sec}秒窗口强平-校验】{state.event_name}："
                            f"账号={account_text} | order_id={order.order_id} | "
                            f"撤单后重查发现已成交（status={latest_status}），跳过 FAK 强平"
                        )
                        cancel_already_terminal = True
                    elif normalized == OrderStatus.CANCELLED.value:
                        order.status = OrderStatus.CANCELLED.value
                        order.raw_status = latest_status
                        state.mark_order(order)
                        cancel_already_terminal = True
                    elif not cancel_outcome.success:
                        # Cancel failed AND the order is still live on the book. We cannot safely
                        # proceed to FAK 平仓 — there is a live order that will eat into the same
                        # token balance, and our FAK would race against it (and likely fail with
                        # "not enough balance" because the tokens are still escrowed). Skip FAK to
                        # avoid double-spending the same tokens. The order will sit until end of
                        # market resolution (or be cancelled by post-event sweep).
                        print(
                            f"【{close_window_sec}秒窗口强平-校验】{state.event_name}："
                            f"账号={account_text} | order_id={order.order_id} | "
                            f"撤单失败且订单仍在簿上（status={latest_status}），跳过 FAK 强平以避免与挂单抢同一份 token"
                        )
                        cancel_already_terminal = True

            if cancel_already_terminal:
                # If it was filled, we are done (no FAK needed). If cancelled, there is no
                # outstanding position to flatten (in the single-side case the entry fill is
                # already gone because the抛售单 was the current order). Skip.
                continue

            # 3) Decide how many shares to FAK 平仓.
            # For single_side_fill_pending_close we use the original entry filled_shares from
            # history; otherwise we use the current order's filled_shares.
            if is_single_side_pending and wallet.wallet_id in entry_filled_by_wallet:
                filled_shares = entry_filled_by_wallet[wallet.wallet_id]
            else:
                filled_shares = 0.0
                try:
                    filled_shares = float(order.filled_shares or order.shares or 0.0) if order else 0.0
                except (TypeError, ValueError):
                    filled_shares = 0.0
            if filled_shares <= 0:
                print(f"【{close_window_sec}秒窗口强平-平仓】{state.event_name}：账号={account_text} | 已成交份额=0，跳过平仓")
                continue
            close_amount = filled_shares * self.fixed_sell_price
            close = self.executor.place_sell(
                wallet=wallet,
                event_name=state.event_name,
                side=order.side if order else OrderSide.UP,
                shares=float(filled_shares),
                price=self.fixed_sell_price,
                clob_token_ids=clob_token_ids,
                fee_rate_bps=fee_rate_bps,
                condition_id=condition_id,
                order_type_override="FAK",
                post_only=False,
            )
            close_snapshot = build_order_snapshot(wallet=wallet, event_name=state.event_name, side=order.side if order else OrderSide.UP, amount_usd=close_amount, operation=OperationType.FORCE_CLOSE, outcome=close, close_price=self.fixed_sell_price)
            close_snapshot.status = OrderStatus.FILLED.value if close.success else OrderStatus.FAILED.value
            state.mark_order(close_snapshot)
            close_result = "成功" if close.success else "失败"
            error_detail = "" if close.success else f" | error={close.error or 'unknown'}"
            fill_detail = ""
            if close.success:
                fill_detail = f" | filled_shares={float(close.filled_shares or 0.0):.4f} | avg_price={float(close.average_fill_price or close.price or 0.0):.4f}"
            print(
                f"【{close_window_sec}秒窗口强平-平仓】{state.event_name}："
                f"账号={account_text} | side={order.side.value if order and order.side else 'UNKNOWN'}"
                f" | 已成交份额={filled_shares:.4f} | 平仓金额={float(close_amount):.4f} | 结果={close_result}{error_detail}{fill_detail}"
            )
            if self.structured_log:
                self.structured_log.record_order(close_snapshot)
        state.flow_state = EventFlowState.FORCE_CLOSE_PLACED
        self._log_state(state, phase=state.flow_state.value, note=f"{close_window_sec}秒窗口强平：已按账号发出撤单与平仓指令")

    def _build_summary(self, state: DualWalletEventState, *, condition_id: str) -> EventResultSummary:
        outcome_payload = self._wait_for_market_outcome(condition_id=condition_id)
        outcome = self._parse_event_outcome(outcome_payload)
        wallet_balances = self._wait_for_balances_to_settle(state=state)

        pnl_by_wallet: dict[str, float] = {}
        for wallet in self.wallets:
            start_balance = self._event_start_balances.get(wallet.wallet_id)
            end_balance = wallet_balances.get(wallet.wallet_id)
            if start_balance is None or end_balance is None:
                pnl_by_wallet[wallet.wallet_id] = 0.0
                continue
            pnl_by_wallet[wallet.wallet_id] = round(float(end_balance) - float(start_balance), 6)

        filled = 0
        cancelled = 0
        force_closed = 0
        for wallet in self.wallets:
            for snapshot in state.get_order_history(wallet.wallet_id):
                if snapshot.status == OrderStatus.FILLED.value:
                    filled += 1
                if snapshot.operation == OperationType.CANCEL and snapshot.status == OrderStatus.CANCELLED.value:
                    cancelled += 1
                if snapshot.operation == OperationType.FORCE_CLOSE and snapshot.status == OrderStatus.FILLED.value:
                    force_closed += 1

        total_pnl = round(sum(pnl_by_wallet.values()), 6)
        return EventResultSummary(
            event_name=state.event_name,
            outcome=outcome,
            total_pnl_usd=total_pnl,
            wallet_pnl_usd={k: round(v, 6) for k, v in pnl_by_wallet.items()},
            wallet_balance_usdc=wallet_balances,
            order_count=sum(len(state.get_order_history(wallet.wallet_id)) for wallet in self.wallets),
            filled_count=filled,
            cancelled_count=cancelled,
            force_closed_count=force_closed,
            is_profit=total_pnl > 0,
            settled_at=datetime.now(timezone.utc),
        )

    def _wait_for_market_outcome(self, *, condition_id: str) -> dict[str, Any] | None:
        deadline = time.monotonic() + max(self.outcome_poll_timeout_sec, self.outcome_poll_interval_sec)
        remaining = deadline - time.monotonic()
        print(f"【等待结算】轮询市场结果，超时 {int(remaining)}s，间隔 {int(self.outcome_poll_interval_sec)}s")
        last_payload: dict[str, Any] | None = None
        last_log_time = 0
        while True:
            payload = fetch_market_outcome(condition_id, slug=None, clob_token_ids=None)
            if isinstance(payload, dict):
                last_payload = payload
                outcome = self._parse_event_outcome(payload)
                if outcome != EventOutcome.UNKNOWN:
                    print(f"【结算确认】市场结果已揭晓: {outcome.value}")
                    return payload
            elapsed = deadline - time.monotonic()
            now = time.monotonic()
            if now - last_log_time >= 30:
                print(f"【等待结算】仍在等待市场结果，剩余 {int(elapsed)}s...")
                last_log_time = now
            if elapsed <= 0:
                print(f"【结算超时】等待市场结果超时，condition_id={condition_id}")
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

    def _wait_for_balances_to_settle(self, *, state: DualWalletEventState) -> dict[str, float | None]:
        required_stable_rounds = max(1, self.settlement_stable_rounds)
        deadline = time.monotonic() + max(self.settlement_poll_timeout_sec, self.settlement_poll_interval_sec)
        remaining = deadline - time.monotonic()
        print(f"【等待结算】等待钱包余额稳定，超时 {int(remaining)}s，间隔 {int(self.settlement_poll_interval_sec)}s，需 {required_stable_rounds} 轮确认")
        stable_rounds = 0
        last_balances: dict[str, float | None] | None = None
        latest_balances: dict[str, float | None] = self._fetch_wallet_balances_after_settlement()
        last_log_time = 0

        while True:
            latest_balances = self._fetch_wallet_balances_after_settlement()
            if self._balances_equal(last_balances, latest_balances):
                stable_rounds += 1
            else:
                stable_rounds = 1
                last_balances = dict(latest_balances)

            if stable_rounds >= required_stable_rounds:
                print(f"【结算完成】{state.event_name}：钱包余额已稳定（{stable_rounds} 轮），按最终余额对账")
                self._log_state(state, phase=state.flow_state.value, note="balances_stabilized", payload={"stable_rounds": stable_rounds, "balances": latest_balances})
                return latest_balances

            elapsed = deadline - time.monotonic()
            now = time.monotonic()
            if now - last_log_time >= 30:
                print(f"【等待结算】余额尚未稳定，第 {stable_rounds}/{required_stable_rounds} 轮，剩余 {int(elapsed)}s...")
                last_log_time = now
            if elapsed <= 0:
                print(f"【结算超时】{state.event_name}：余额稳定等待超时，按当前余额对账")
                self._log_state(state, phase=state.flow_state.value, note="balance_stabilization_timeout", payload={"stable_rounds": stable_rounds, "balances": latest_balances})
                return latest_balances

            time.sleep(max(1, self.settlement_poll_interval_sec))

    def _balances_equal(self, left: dict[str, float | None] | None, right: dict[str, float | None] | None) -> bool:
        if left is None or right is None:
            return False
        keys = set(left) | set(right)
        for key in keys:
            left_value = left.get(key)
            right_value = right.get(key)
            if left_value is None or right_value is None:
                if left_value != right_value:
                    return False
                continue
            if abs(float(left_value) - float(right_value)) > 1e-6:
                return False
        return True

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
