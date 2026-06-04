"""
Dual-wallet event trading domain models and lifecycle helpers.

This module keeps the new business flow separate from the legacy preopen
state machine. It only models event-level coordination, wallet identity,
order intents, and lifecycle state transitions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import random
from typing import Iterable


class WalletRole(str, Enum):
    A = "a"
    B = "b"


class OrderSide(str, Enum):
    UP = "UP"
    DOWN = "DOWN"


class OperationType(str, Enum):
    PLACE = "挂单"
    CANCEL = "取消挂单"
    SELL = "挂卖"
    FORCE_CLOSE = "平仓"


class EventOutcome(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    UNKNOWN = "UNKNOWN"


class EventFlowState(str, Enum):
    NEW = "new"
    ENTRY_PLACED = "entry_placed"
    ENTRY_PARTIAL = "entry_partial"
    ENTRY_CONFIRMED = "entry_confirmed"
    HEDGE_CANCELLED = "hedge_cancelled"
    SELL_PLACED = "sell_placed"
    FORCE_CLOSE_PLACED = "force_close_placed"
    SETTLED = "settled"
    STOPPED = "stopped"


@dataclass(frozen=True)
class WalletIdentity:
    wallet_id: str
    wallet_name: str
    private_key_env: str
    role: WalletRole


@dataclass
class OrderSnapshot:
    wallet: WalletIdentity
    event_name: str
    side: OrderSide
    amount_usd: float
    operation: OperationType
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    order_id: str | None = None
    price: float | None = None
    shares: float | None = None
    status: str | None = None
    close_price: float | None = None

    @property
    def timestamp_cn(self) -> str:
        return self.timestamp.astimezone().strftime("%m月%d日 %H:%M:%S")


@dataclass
class EventResultSummary:
    event_name: str
    outcome: EventOutcome = EventOutcome.UNKNOWN
    total_pnl_usd: float = 0.0
    wallet_pnl_usd: dict[str, float] = field(default_factory=dict)
    order_count: int = 0
    filled_count: int = 0
    cancelled_count: int = 0
    force_closed_count: int = 0
    is_profit: bool = False
    settled_at: datetime | None = None


@dataclass
class DualWalletEventState:
    event_name: str
    event_id: str
    start_time: datetime
    end_time: datetime
    close_price: float
    close_window_sec: int
    x_timeout_sec: int
    flow_state: EventFlowState = EventFlowState.NEW
    wallet_orders: dict[str, OrderSnapshot] = field(default_factory=dict)
    wallet_status: dict[str, str] = field(default_factory=dict)
    first_fill_wallet_id: str | None = None
    second_fill_wallet_id: str | None = None
    outcome: EventOutcome = EventOutcome.UNKNOWN
    result_summary: EventResultSummary | None = None
    halted_reason: str | None = None
    side_by_wallet_id: dict[str, OrderSide] = field(default_factory=dict)

    def mark_order(self, snapshot: OrderSnapshot) -> None:
        self.wallet_orders[snapshot.wallet.wallet_id] = snapshot
        self.wallet_status[snapshot.wallet.wallet_id] = snapshot.status or snapshot.operation.value

    def get_order(self, wallet_id: str) -> OrderSnapshot | None:
        return self.wallet_orders.get(wallet_id)

    def count_filled(self) -> int:
        return sum(1 for s in self.wallet_status.values() if s == "filled")

    def active_wallet_ids(self) -> list[str]:
        return [wallet_id for wallet_id, status in self.wallet_status.items() if status not in {"cancelled", "closed", "settled"}]

    def is_within_force_close_window(self, now: datetime) -> bool:
        return (self.end_time - now).total_seconds() <= self.close_window_sec

    def remaining_to_start(self, now: datetime) -> float:
        return (self.start_time - now).total_seconds()

    def remaining_to_end(self, now: datetime) -> float:
        return (self.end_time - now).total_seconds()

    def should_halt(self) -> bool:
        return self.flow_state == EventFlowState.STOPPED or bool(self.halted_reason)


def build_wallet_identities(*, wallet_a_private_key_env: str, wallet_b_private_key_env: str) -> list[WalletIdentity]:
    return [
        WalletIdentity(
            wallet_id="wallet_a",
            wallet_name="钱包A",
            private_key_env=wallet_a_private_key_env,
            role=WalletRole.A,
        ),
        WalletIdentity(
            wallet_id="wallet_b",
            wallet_name="钱包B",
            private_key_env=wallet_b_private_key_env,
            role=WalletRole.B,
        ),
    ]


def assign_event_sides(wallets: list[WalletIdentity]) -> dict[str, OrderSide]:
    if len(wallets) != 2:
        raise ValueError("dual_wallet strategy requires exactly two wallets")
    first_up = bool(random.getrandbits(1))
    return {
        wallets[0].wallet_id: OrderSide.UP if first_up else OrderSide.DOWN,
        wallets[1].wallet_id: OrderSide.DOWN if first_up else OrderSide.UP,
    }


def format_operation_timestamp(ts: datetime | None = None) -> str:
    ts = ts or datetime.now()
    return ts.strftime("%m月%d日 %H:%M:%S")


def iter_wallet_orders(state: DualWalletEventState) -> Iterable[OrderSnapshot]:
    return state.wallet_orders.values()
