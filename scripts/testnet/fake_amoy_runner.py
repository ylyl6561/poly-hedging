"""
fake_amoy_runner.py
===================

End-to-end runner for the dual-wallet state machine in "fake-amoy" mode.

Why this exists
---------------
Polymarket CLOB V2 has no public sandbox/testnet host. Running the full
state machine on mainnet costs real money and risks real losses from
strategy bugs. This runner exercises the *real* `DualWalletEventStrategy`
code path (place → wait → cancel/sell/force_close → settle) with three
classes of mocks:

  1. Time + polls       — `_wait_and_handle_partials`, `_force_close_if_needed`,
                          `_wait_for_market_outcome`, `_wait_for_balances_to_settle`
                          are monkey-patched to deterministic, in-test drivers
                          that read from the scenario's `status_script` and
                          `fake_outcome`. The actual state transitions
                          (entry_placed → single_side_fill_pending_close →
                          force_close_placed → settled) are NOT mocked.

  2. Executor           — `DualWalletExecutor(dry_run=True)` already returns
                          mock responses via `direct_polymarket_trade(mock=True)`.
                          No real network calls, no real signatures.

  3. Wallet balance     — `get_wallet_usdc_balance` is patched to return
                          preset starting/ending balances so PnL is computed
                          against deterministic numbers.

This is *not* a unit test of the wait loop. This is an integration test of
the state machine + executor + log/CSV/Excel export pipeline.

Usage
-----
    .venv/bin/python scripts/testnet/fake_amoy_runner.py single_side_fill
    .venv/bin/python scripts/testnet/fake_amoy_runner.py all

Outputs are written to `main/runs/fake_amoy_<scenario>_<timestamp>/` with
the same artifacts that the live trader produces: `output.log`,
`structured_output.json`, `orders.csv`, `results.csv`, `trades.csv`,
`trades.xlsx`.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Test account stubs
# ---------------------------------------------------------------------------

import types

from dataclasses import dataclass


@dataclass(frozen=True)
class _StubAccountContext:
    account_id: str
    label: str
    private_key_env: str
    funder_env: str | None
    proxy_address_env: str | None
    private_key: str
    wallet_address: str
    funder_address: str | None
    proxy_address: str | None
    host: str
    chain_id: int
    signature_type: int
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class _StubClientCacheKey:
    account_id: str
    wallet_address: str
    funder_address: str | None
    proxy_address: str | None
    host: str
    chain_id: int
    signature_type: int


@dataclass
class _StubClientRecord:
    client: object
    cache_key: _StubClientCacheKey
    created_at: float
    last_used_at: float


class _StubRegistry:
    def __init__(self):
        self._accounts: list[_StubAccountContext] = []

    def set_accounts(self, accounts: list[_StubAccountContext]) -> None:
        self._accounts = list(accounts)

    def list_accounts(self):
        return list(self._accounts)

    def accounts_with_tag(self, tag: str):
        return [account for account in self._accounts if tag in account.tags]


_stub_registry = _StubRegistry()

_accounts_stub = types.ModuleType("accounts")
_accounts_stub.AccountContext = _StubAccountContext
_accounts_stub.ClientCacheKey = _StubClientCacheKey
_accounts_stub.ClientRecord = _StubClientRecord
_accounts_stub.get_account_registry = lambda: _stub_registry
sys.modules.setdefault("accounts", _accounts_stub)


# ---------------------------------------------------------------------------
# Strategy + log setup
# ---------------------------------------------------------------------------

import strategy.dual_wallet_event_strategy as strategy_module
from state.structured_log import StructuredRunLog
from strategy.dual_wallet_event_strategy import DualWalletEventStrategy
from strategy.dual_wallet_models import (
    DualWalletEventState,
    OrderSide,
    assign_event_sides,
    build_wallet_identities,
)
from strategy.dual_wallet_executor import DualWalletExecutor
from accounts import get_account_registry  # noqa: E402  (uses the stub we injected above)


SCENARIOS_PATH = Path(__file__).resolve().parent / "scenarios.json"


def _make_account(account_id: str, label: str, private_key_env: str, signature_type: int = 1) -> _StubAccountContext:
    wallet_address = f"0x{account_id[-8:].rjust(40, '0')}"
    return _StubAccountContext(
        account_id=account_id,
        label=label,
        private_key_env=private_key_env,
        funder_env=None,
        proxy_address_env=None,
        private_key="TEST_PRIVATE_KEY",
        wallet_address=wallet_address,
        funder_address=wallet_address,
        proxy_address=None,
        host="https://clob.polymarket.com",
        chain_id=137,
        signature_type=signature_type,
        tags=("dual_wallet", "fake_amoy_test"),
    )


def _build_test_config(scenario: dict) -> dict:
    return {
        "dual_wallet_entry_timeout_sec": int(scenario.get("entry_timeout_sec", 3)),
        "dual_wallet_force_close_window_sec": int(scenario.get("force_close_window_sec", 60)),
        "dual_wallet_fixed_sell_price": 0.76,
        "dual_wallet_entry_up_price": 0.5,
        "dual_wallet_entry_down_price": 0.5,
        "dual_wallet_entry_amount_usd": 10.0,
        "dual_wallet_max_consecutive_losses": 99,
        "dual_wallet_poll_interval_sec": 1,
        "dual_wallet_outcome_poll_interval_sec": 1,
        "dual_wallet_outcome_poll_timeout_sec": 5,
        "dual_wallet_settlement_poll_interval_sec": 1,
        "dual_wallet_settlement_poll_timeout_sec": 5,
        "dual_wallet_settlement_stable_rounds": 1,
        "dual_wallet_event_query_limit": 1,
        "execution_route": "direct_clob",
        "order_type": "GTC",
        "polymarket_accounts": [],
        "dual_wallet_dry_run_status_script": scenario["status_script"],
    }


# ---------------------------------------------------------------------------
# Mock drivers — replace the wait/poll methods
# ---------------------------------------------------------------------------

def _make_drivers(scenario: dict, start_balances: dict[str, float], end_balances: dict[str, float], outcome: str):
    """Return a dict of {method_name: replacement} for the wait/poll methods."""

    # Determine the expected outcome from the scenario config or default UP.
    if outcome not in {"UP", "DOWN"}:
        outcome = "UP"

    outcome_payload = {"success": True, "condition_id": "fake", "outcome": outcome, "settled": True}

    def fake_wait_for_market_outcome(self, *, condition_id):
        return outcome_payload

    def fake_wait_for_balances_to_settle(self, *, state):
        # Return the prescribed end balances immediately. (Real PnL signal here
        # is the difference vs the start balances we passed in.)
        return dict(end_balances)

    def fake_wait_and_handle_partials(self, state, *, clob_token_ids, fee_rate_bps, condition_id):
        """Drive the state machine transitions once, then return.

        Strategy: refresh order statuses once (status_script advances the
        cursor). Then check fill state and let the natural branches fire
        exactly the same way the real method does. We DO call into
        _cancel_and_sell_stale, _force_close, etc. so all real branches
        run — we just don't loop or sleep.
        """
        from strategy.dual_wallet_models import EventFlowState, OrderSide, OrderStatus

        # If event is already over (no time to wait), set the trigger to
        # force_close_window so the next step runs the close logic.
        now = datetime.now(timezone.utc)
        remaining_to_end = state.remaining_to_end(now)
        if remaining_to_end <= 0:
            state.trigger_reason = "event_already_ended"
            state.trigger_detail = f"remaining_to_end_sec={int(remaining_to_end)}"
            return

        # Refresh once — this advances the status_script cursor.
        self._refresh_entry_order_statuses(state)

        up_wallet = next((w for w in self.wallets if state.side_by_wallet_id.get(w.wallet_id) == OrderSide.UP), None)
        down_wallet = next((w for w in self.wallets if state.side_by_wallet_id.get(w.wallet_id) == OrderSide.DOWN), None)
        up = state.get_order(up_wallet.wallet_id) if up_wallet else None
        down = state.get_order(down_wallet.wallet_id) if down_wallet else None
        up_filled = bool(up and up.status == OrderStatus.FILLED.value and (up.filled_shares or up.shares))
        down_filled = bool(down and down.status == OrderStatus.FILLED.value and (down.filled_shares or down.shares))

        if up_filled and down_filled:
            from strategy.dual_wallet_models import ExecutionOutcome
            state.first_fill_wallet_id = up_wallet.wallet_id
            state.second_fill_wallet_id = down_wallet.wallet_id
            state.trigger_reason = "both_sides_filled"
            state.trigger_detail = f"up_filled_shares={up.filled_shares or up.shares};down_filled_shares={down.filled_shares or down.shares}"
            state.flow_state = EventFlowState.ENTRY_CONFIRMED
            self._log_state(state, phase=state.flow_state.value, note="both sides filled (fake_amoy)")
            return

        if up_filled ^ down_filled:
            live_wallet = up_wallet if up_filled else down_wallet
            stale_wallet = down_wallet if up_filled else up_wallet
            stale_snapshot = down if up_filled else up
            live_snapshot = up if up_filled else down
            from strategy.dual_wallet_models import ExecutionOutcome
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
                    raw={"source": "fake_amoy_state"},
                ),
                clob_token_ids=clob_token_ids,
                fee_rate_bps=fee_rate_bps,
                condition_id=condition_id,
            )
            return

        # No fills yet — set the trigger_reason based on remaining time vs
        # force_close_window. This mirrors the real method's exit logic so
        # the next step (_force_close_if_needed) takes the right branch.
        close_window_sec = max(1, int(self.force_close_window_sec))
        now = datetime.now(timezone.utc)
        remaining_to_end = (state.end_time - now).total_seconds()
        if remaining_to_end <= close_window_sec:
            state.trigger_reason = "force_close_window"
            state.trigger_detail = f"remaining_to_end_sec={int(remaining_to_end)};close_window_sec={close_window_sec}"
        else:
            state.trigger_reason = "wait_timeout_no_clear_fill"
            state.trigger_detail = (
                f"entry_timeout_sec={self.entry_timeout_sec};"
                f"remaining_to_end_sec={int(remaining_to_end)};"
                f"no_clear_fill"
            )
        self._log_state(state, phase=state.flow_state.value, note=f"no fills (fake_amoy), trigger={state.trigger_reason}")

    def fake_force_close_if_needed(self, state, *, clob_token_ids, fee_rate_bps, condition_id):
        if state.flow_state == strategy_module.EventFlowState.SETTLED:
            return
        if state.trigger_reason not in {"force_close_window", "single_side_fill_pending_close"}:
            return
        # Real _force_close: no time-gating, just execute.
        self._force_close(state, clob_token_ids=clob_token_ids, fee_rate_bps=fee_rate_bps, condition_id=condition_id)

    return {
        "_wait_and_handle_partials": fake_wait_and_handle_partials,
        "_force_close_if_needed": fake_force_close_if_needed,
        "_wait_for_market_outcome": fake_wait_for_market_outcome,
        "_wait_for_balances_to_settle": fake_wait_for_balances_to_settle,
    }


# ---------------------------------------------------------------------------
# Patches — replace time.sleep + balance fetch + outcome fetch
# ---------------------------------------------------------------------------

@contextmanager
def _patched_runtime(start_balances: dict[str, float], end_balances: dict[str, float], outcome: str):
    """Patch global time.sleep, fetch_market_outcome, get_wallet_usdc_balance."""
    original_sleep = strategy_module.time.sleep

    def fake_sleep(seconds: float) -> None:
        return None  # no real sleeps

    def fake_fetch_outcome(condition_id, slug=None, clob_token_ids=None):
        return {"success": True, "condition_id": condition_id, "outcome": outcome, "settled": True}

    # Per-account balance state machine: first N calls return start_balances,
    # subsequent calls return end_balances. We achieve this with a counter
    # keyed by account_id, so the start-snapshot uses starts, the
    # settlement-snapshot uses ends.
    call_counts: dict[str, int] = {}

    def fake_get_balance(*, account):
        call_counts[account.account_id] = call_counts.get(account.account_id, 0) + 1
        # Calls 1 & 2 are _log_wallet_account_data + _snapshot_start_balances
        # → return start balances. Calls 3+ are settlement polling → return
        # end balances. (This mirrors the real flow: snapshot, then post-
        # event polling, then summary.)
        if call_counts[account.account_id] <= 2:
            balance = start_balances.get(account.account_id, 100.0)
        else:
            balance = end_balances.get(account.account_id, 100.0)
        return {"success": True, "balance_usdc": balance}

    strategy_module.time.sleep = fake_sleep
    strategy_module.fetch_market_outcome = fake_fetch_outcome
    strategy_module.get_wallet_usdc_balance = fake_get_balance
    try:
        yield
    finally:
        strategy_module.time.sleep = original_sleep
        # We re-resolve the original symbols to avoid leaving the module in
        # a bad state. The original functions are still bound in api module.
        from api import fetch_market_outcome as _orig_fetch_outcome
        from api import get_wallet_usdc_balance as _orig_balance
        strategy_module.fetch_market_outcome = _orig_fetch_outcome
        strategy_module.get_wallet_usdc_balance = _orig_balance


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_scenario(name: str, scenario: dict, base_run_dir: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    run_dir = base_run_dir / f"fake_amoy_{name}_{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    structured_log = StructuredRunLog(run_dir, mode="fake_amoy", log_file_name="output.log")

    accounts = [
        _make_account("wallet_a", "钱包A", "WALLET_PRIVATE_KEY_A", signature_type=1),
        _make_account("wallet_b", "钱包B", "WALLET_PRIVATE_KEY_B", signature_type=3),
    ]
    _stub_registry.set_accounts(accounts)
    cfg = _build_test_config(scenario)

    strategy = DualWalletEventStrategy(
        run_folder=run_dir, dry_run=True, config=cfg, structured_log=structured_log,
    )
    # Don't let strategy._select_two_dual_wallet_accounts re-read registry
    # at __init__ (it already did with stubs in place). Use what it picked.
    strategy.selected_accounts = accounts
    strategy.wallets = build_wallet_identities(accounts)

    # Build scenario event times. start_time must be far enough in the future
    # to pass run_event's `min_seconds_before_start` guard. We anchor
    # start_time = now + 60s and end_time = start_time + scenario seconds_to_end.
    margin_sec = 60
    start_time = datetime.now(timezone.utc) + timedelta(seconds=margin_sec)
    end_time = start_time + timedelta(seconds=int(scenario["seconds_to_end"]))
    event_name = f"FAKE-AMOY::{name}"
    event_id = f"fake-amoy-{name}-{int(time.time())}"

    # Pick outcome: cycle UP/DOWN across scenarios for variety.
    outcome = "UP" if name in {"single_side_fill", "force_close_window"} else "DOWN"
    start_balances = {"wallet_a": 100.0, "wallet_b": 100.0}
    end_balances = {"wallet_a": 100.0, "wallet_b": 100.0}

    # Patch the strategy's wait/poll methods (bound-method style).
    drivers = _make_drivers(scenario, start_balances, end_balances, outcome)

    from unittest.mock import patch as _patch
    with _patch.object(DualWalletEventStrategy, "_wait_and_handle_partials", drivers["_wait_and_handle_partials"]), \
         _patch.object(DualWalletEventStrategy, "_force_close_if_needed", drivers["_force_close_if_needed"]), \
         _patch.object(DualWalletEventStrategy, "_wait_for_market_outcome", drivers["_wait_for_market_outcome"]), \
         _patch.object(DualWalletEventStrategy, "_wait_for_balances_to_settle", drivers["_wait_for_balances_to_settle"]), \
         _patched_runtime(start_balances, end_balances, outcome):
        summary = strategy.run_event(
            event_name=event_name,
            event_id=event_id,
            start_time=start_time,
            end_time=end_time,
            clob_token_ids=["fake-up-token", "fake-down-token"],
            fee_rate_bps=0,
            condition_id=event_id,
            amount_usd=float(cfg["dual_wallet_entry_amount_usd"]),
            up_price=float(cfg["dual_wallet_entry_up_price"]),
            down_price=float(cfg["dual_wallet_entry_down_price"]),
        )
        structured_log.record_result(summary)
        structured_log.flush()

    # Write scenario result JSON
    payload = {
        "scenario": name,
        "description": scenario.get("description", ""),
        "event_id": event_id,
        "event_name": event_name,
        "outcome": summary.outcome.value,
        "total_pnl_usd": summary.total_pnl_usd,
        "wallet_pnl_usd": summary.wallet_pnl_usd,
        "wallet_balance_usdc": summary.wallet_balance_usdc,
        "order_count": summary.order_count,
        "filled_count": summary.filled_count,
        "cancelled_count": summary.cancelled_count,
        "force_closed_count": summary.force_closed_count,
        "is_profit": summary.is_profit,
        "settled_at": summary.settled_at.isoformat() if summary.settled_at else None,
        "expected_trigger_reason": scenario.get("expected_trigger_reason"),
        "expected_filled_count": scenario.get("expected_filled_count"),
        "run_dir": str(run_dir),
    }
    (run_dir / "scenario_result.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return run_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Run dual-wallet state machine end-to-end in fake-amoy mode")
    parser.add_argument("scenario", help="Scenario name from scenarios.json, or 'all'")
    parser.add_argument("--run-dir", default="main/runs", help="Base directory for run artifacts")
    args = parser.parse_args()

    scenarios_file = json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))
    scenarios = scenarios_file["scenarios"]

    if args.scenario not in scenarios and args.scenario != "all":
        print(f"Unknown scenario: {args.scenario}. Available: {list(scenarios.keys())} or 'all'", file=sys.stderr)
        return 1

    base_run_dir = Path(args.run_dir)
    base_run_dir.mkdir(parents=True, exist_ok=True)

    names = list(scenarios.keys()) if args.scenario == "all" else [args.scenario]
    results: list[tuple[str, Path, dict]] = []
    for name in names:
        scenario = scenarios[name]
        run_dir = run_scenario(name, scenario, base_run_dir)
        result = json.loads((run_dir / "scenario_result.json").read_text(encoding="utf-8"))
        results.append((name, run_dir, result))
        print(f"[OK] {name} -> {run_dir}")

    # Cross-scenario validation against expected values.
    print()
    print("=" * 70)
    print(" Scenario validation")
    print("=" * 70)
    all_pass = True
    for name, _, result in results:
        scenario = scenarios[name]
        exp_reason = scenario.get("expected_trigger_reason")
        exp_filled = scenario.get("expected_filled_count")
        actual_reason = None
        # trigger_reason is not in EventResultSummary; it lives on the state
        # machine. We pull it back from the structured_output.json file.
        run_dir = Path(result["run_dir"])
        structured = json.loads((run_dir / "structured_output.json").read_text(encoding="utf-8"))
        for ev in structured.get("events", []):
            payload = ev.get("payload", {}) or {}
            note = payload.get("note", "") or ""
            for kw in ("single_side_fill_pending_close", "both_sides_filled", "force_close_window", "wait_timeout_no_clear_fill", "event_already_ended"):
                if kw in str(payload) or kw in note:
                    actual_reason = kw
        actual_filled = result["filled_count"]
        ok = True
        if exp_reason and exp_reason not in str(actual_reason or ""):
            print(f"  ✗ {name}: expected trigger_reason~={exp_reason!r}, got {actual_reason!r}")
            ok = False
        if exp_filled is not None and actual_filled != exp_filled:
            print(f"  ✗ {name}: expected filled_count={exp_filled}, got {actual_filled}")
            ok = False
        if ok:
            print(f"  ✓ {name}: trigger={actual_reason} | filled={actual_filled} | pnl={result['total_pnl_usd']}")
        else:
            all_pass = False
    print("=" * 70)
    if all_pass:
        print("All scenarios matched expectations.")
    else:
        print("Some scenarios diverged — see above.")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
