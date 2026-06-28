from __future__ import annotations

import argparse
import json
import sys
import types
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

_project_root = Path(__file__).parent.parent.resolve()
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

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

import strategy.dual_wallet_event_strategy as strategy_module
from state.structured_log import StructuredRunLog
from strategy.dual_wallet_event_strategy import DualWalletEventStrategy
from strategy.dual_wallet_models import DualWalletEventState, OrderSide, build_wallet_identities


def make_account(account_id: str, label: str, private_key_env: str, signature_type: int = 1) -> _StubAccountContext:
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
        tags=("dual_wallet", "test"),
    )


def build_test_config(status_script: dict, *, entry_timeout_sec: int = 3, force_close_window_sec: int = 60) -> dict:
    return {
        "dual_wallet_entry_timeout_sec": entry_timeout_sec,
        "dual_wallet_force_close_window_sec": force_close_window_sec,
        "dual_wallet_fixed_sell_price": 0.76,
        "dual_wallet_entry_up_price": 0.5,
        "dual_wallet_entry_down_price": 0.5,
        "dual_wallet_entry_shares": 10.0,
        "dual_wallet_max_consecutive_losses": 99,
        "dual_wallet_poll_interval_sec": 1,
        "dual_wallet_outcome_poll_interval_sec": 1,
        "dual_wallet_outcome_poll_timeout_sec": 1,
        "dual_wallet_settlement_poll_interval_sec": 1,
        "dual_wallet_settlement_poll_timeout_sec": 1,
        "dual_wallet_settlement_stable_rounds": 1,
        "dual_wallet_event_query_limit": 1,
        "execution_route": "direct_clob",
        "order_type": "GTC",
        "polymarket_accounts": [],
        "dual_wallet_dry_run_status_script": status_script,
    }


@contextmanager
def patched_runtime(*, outcome: str = "UP", balances: dict[str, float] | None = None):
    original_fetch_outcome = strategy_module.fetch_market_outcome
    original_get_balance = strategy_module.get_wallet_usdc_balance

    def fake_fetch_market_outcome(condition_id, slug=None, clob_token_ids=None):
        return {"success": True, "condition_id": condition_id, "outcome": outcome}

    def fake_get_wallet_usdc_balance(*, account):
        balance = (balances or {}).get(account.account_id, 100.0)
        return {"success": True, "balance_usdc": balance}

    strategy_module.fetch_market_outcome = fake_fetch_market_outcome
    strategy_module.get_wallet_usdc_balance = fake_get_wallet_usdc_balance
    try:
        yield
    finally:
        strategy_module.fetch_market_outcome = original_fetch_outcome
        strategy_module.get_wallet_usdc_balance = original_get_balance


SCENARIOS = {
    "step6_single_side_fill": {
        "description": "单边成交 -> 撤另一侧 -> 对成交侧收口",
        "status_script": {
            "wallet_a": {"by_side": {"UP": ["submitted", "filled"], "DOWN": ["submitted", "submitted"]}, "fill_price": 0.5, "filled_amount_usd": 10.0},
            "wallet_b": {"by_side": {"UP": ["submitted", "submitted"], "DOWN": ["submitted", "submitted"]}, "fill_price": 0.5, "filled_amount_usd": 10.0},
        },
        "seconds_to_end": 180,
    },
    "step7_both_sides_fill": {
        "description": "双边成交确认",
        "status_script": {
            "wallet_a": {"by_side": {"UP": ["submitted", "filled"], "DOWN": ["submitted", "filled"]}, "fill_price": 0.5, "filled_amount_usd": 10.0},
            "wallet_b": {"by_side": {"UP": ["submitted", "filled"], "DOWN": ["submitted", "filled"]}, "fill_price": 0.5, "filled_amount_usd": 10.0},
        },
        "seconds_to_end": 180,
    },
    "step8_force_close": {
        "description": "等待超时 -> 进入强平窗口 -> 逐账号撤单/平仓",
        "status_script": {
            "wallet_a": {"by_side": {"UP": ["submitted", "submitted", "submitted"], "DOWN": ["submitted", "submitted", "submitted"]}, "fill_price": 0.5, "filled_amount_usd": 10.0},
            "wallet_b": {"by_side": {"UP": ["submitted", "submitted", "submitted"], "DOWN": ["submitted", "submitted", "submitted"]}, "fill_price": 0.5, "filled_amount_usd": 10.0},
        },
        "seconds_to_end": 45,
        "entry_timeout_sec": 2,
        "force_close_window_sec": 60,
    },
}


def run_scenario(name: str, *, base_run_dir: Path) -> Path:
    scenario = SCENARIOS[name]
    run_dir = base_run_dir / name
    run_dir.mkdir(parents=True, exist_ok=True)
    structured_log = StructuredRunLog(run_dir, mode="dryrun-test", log_file_name="output.log")

    accounts = [
        make_account("wallet_a", "钱包A", "WALLET_PRIVATE_KEY_A", signature_type=1),
        make_account("wallet_b", "钱包B", "WALLET_PRIVATE_KEY_B", signature_type=3),
    ]
    _stub_registry.set_accounts(accounts)
    cfg = build_test_config(
        scenario["status_script"],
        entry_timeout_sec=int(scenario.get("entry_timeout_sec", 3)),
        force_close_window_sec=int(scenario.get("force_close_window_sec", 60)),
    )
    strategy = DualWalletEventStrategy(run_folder=run_dir, dry_run=True, config=cfg, structured_log=structured_log)
    strategy.selected_accounts = accounts
    strategy.wallets = build_wallet_identities(accounts)

    end_time = datetime.now(timezone.utc) + timedelta(seconds=int(scenario["seconds_to_end"]))
    start_time = datetime.now(timezone.utc) + timedelta(seconds=int(scenario.get("entry_grace_sec", 1)))
    state = DualWalletEventState(
        event_name=f"TEST::{name}",
        event_id=f"test-{name}",
        start_time=start_time,
        end_time=end_time,
        close_price=0.76,
        close_window_sec=int(cfg["dual_wallet_force_close_window_sec"]),
        x_timeout_sec=int(cfg["dual_wallet_entry_timeout_sec"]),
    )
    # Force a deterministic side assignment so status_script (keyed by side) is reproducible.
    side_by_wallet_id = {
        accounts[0].account_id: OrderSide.UP,
        accounts[1].account_id: OrderSide.DOWN,
    }
    state.side_by_wallet_id = side_by_wallet_id

    with patched_runtime(outcome="UP", balances={"wallet_a": 105.0, "wallet_b": 95.0}):
        strategy._snapshot_start_balances()
        strategy._place_initial_legs(
            state,
            clob_token_ids=["yes-token", "no-token"],
            fee_rate_bps=0,
            condition_id=state.event_id,
            amount_usd=float(cfg["dual_wallet_entry_amount_usd"]),
            up_price=float(cfg["dual_wallet_entry_up_price"]),
            down_price=float(cfg["dual_wallet_entry_down_price"]),
        )
        strategy._wait_and_handle_partials(state, clob_token_ids=["yes-token", "no-token"], fee_rate_bps=0, condition_id=state.event_id)
        strategy._force_close_if_needed(state, clob_token_ids=["yes-token", "no-token"], fee_rate_bps=0, condition_id=state.event_id)
        summary = strategy._build_summary(state, condition_id=state.event_id)
        state.result_summary = summary
        strategy._export_summary(state, summary)
        structured_log.record_result(summary)
        structured_log.flush()

    payload = {
        "scenario": name,
        "description": scenario["description"],
        "flow_state": state.flow_state.value,
        "trigger_reason": state.trigger_reason,
        "trigger_detail": state.trigger_detail,
        "first_fill_wallet_id": state.first_fill_wallet_id,
        "second_fill_wallet_id": state.second_fill_wallet_id,
        "summary": {
            "outcome": summary.outcome.value,
            "total_pnl_usd": summary.total_pnl_usd,
            "filled_count": summary.filled_count,
            "cancelled_count": summary.cancelled_count,
            "force_closed_count": summary.force_closed_count,
            "wallet_pnl_usd": summary.wallet_pnl_usd,
        },
    }
    (run_dir / "scenario_result.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return run_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Local no-order tests for dual wallet Step 6/7/8")
    parser.add_argument("scenario", choices=[*SCENARIOS.keys(), "all"], help="Test scenario to run")
    parser.add_argument("--run-dir", default="main/runs/local_step_tests", help="Directory for test artifacts")
    args = parser.parse_args()

    base_run_dir = Path(args.run_dir)
    scenario_names = list(SCENARIOS.keys()) if args.scenario == "all" else [args.scenario]
    for name in scenario_names:
        run_dir = run_scenario(name, base_run_dir=base_run_dir)
        print(f"[OK] {name} -> {run_dir}")


if __name__ == "__main__":
    main()
