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
from collections import deque
import random
from typing import Iterable

from accounts import AccountContext


class WalletRole(str, Enum):
    A = "a"
    B = "b"


class OrderSide(str, Enum):
    UP = "UP"
    DOWN = "DOWN"

    @property
    def token_index(self) -> int:
        """clob_token_ids[0]=UP/YES, clob_token_ids[1]=DOWN/NO."""
        return 0 if self == OrderSide.UP else 1

    @classmethod
    def from_string(cls, value: str) -> "OrderSide":
        """Parse 'up'/'yes'/'UP'/'YES' etc into OrderSide, defaulting to UP."""
        lower = value.lower()
        if lower in ("up", "yes"):
            return cls.UP
        if lower in ("down", "no"):
            return cls.DOWN
        raise ValueError(f"Unknown side value: {value!r}")

    def to_api_str(self) -> str:
        """Return the string expected by Polymarket CLOB API (yes/no)."""
        return "yes" if self == OrderSide.UP else "no"


class SideTokenIndex:
    """Canonical constants for clob_token_ids array indexing."""

    UP = 0    # clob_token_ids[0] = YES token
    DOWN = 1  # clob_token_ids[1] = NO token


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


class OrderStatus(str, Enum):
    SUBMITTED = "submitted"
    FILLED = "filled"
    CANCELLED = "cancelled"
    FAILED = "failed"
    CLOSED = "closed"


@dataclass(frozen=True)
class WalletIdentity:
    wallet_id: str
    wallet_name: str
    role: WalletRole
    account: AccountContext


@dataclass
class OrderSnapshot:
    wallet: WalletIdentity
    event_name: str
    side: OrderSide
    amount_usd: float
    operation: OperationType
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    order_id: str | None = None
    token_id: str | None = None
    condition_id: str | None = None
    price: float | None = None
    shares: float | None = None
    status: str | None = None
    close_price: float | None = None
    filled_amount_usd: float | None = None
    filled_shares: float | None = None
    average_fill_price: float | None = None
    raw_status: str | None = None
    error: str | None = None

    @property
    def timestamp_cn(self) -> str:
        return self.timestamp.astimezone().strftime("%m月%d日 %H:%M:%S")


@dataclass
class EventResultSummary:
    event_name: str
    outcome: EventOutcome = EventOutcome.UNKNOWN
    total_pnl_usd: float = 0.0
    wallet_pnl_usd: dict[str, float] = field(default_factory=dict)
    wallet_balance_usdc: dict[str, float | None] = field(default_factory=dict)
    order_count: int = 0
    filled_count: int = 0
    cancelled_count: int = 0
    force_closed_count: int = 0
    is_profit: bool = False
    settled_at: datetime | None = None


@dataclass
class LossWindowTracker:
    recent_results: deque[bool] = field(default_factory=lambda: deque(maxlen=5))

    def record(self, is_profit: bool) -> None:
        self.recent_results.append(bool(is_profit))

    def consecutive_losses(self) -> int:
        count = 0
        for is_profit in reversed(self.recent_results):
            if is_profit:
                break
            count += 1
        return count

    def should_halt(self, max_consecutive_losses: int) -> bool:
        if max_consecutive_losses <= 0:
            return False
        return self.consecutive_losses() >= max_consecutive_losses


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
    wallet_order_history: dict[str, list[OrderSnapshot]] = field(default_factory=dict)
    wallet_status: dict[str, str] = field(default_factory=dict)
    first_fill_wallet_id: str | None = None
    second_fill_wallet_id: str | None = None
    outcome: EventOutcome = EventOutcome.UNKNOWN
    result_summary: EventResultSummary | None = None
    halted_reason: str | None = None
    side_by_wallet_id: dict[str, OrderSide] = field(default_factory=dict)
    trigger_reason: str | None = None
    trigger_detail: str | None = None

    def mark_order(self, snapshot: OrderSnapshot) -> None:
        history = self.wallet_order_history.setdefault(snapshot.wallet.wallet_id, [])
        history.append(snapshot)
        self.wallet_orders[snapshot.wallet.wallet_id] = snapshot
        self.wallet_status[snapshot.wallet.wallet_id] = snapshot.status or snapshot.operation.value

    def get_order(self, wallet_id: str) -> OrderSnapshot | None:
        return self.wallet_orders.get(wallet_id)

    def get_order_history(self, wallet_id: str) -> list[OrderSnapshot]:
        return list(self.wallet_order_history.get(wallet_id, []))

    def count_filled(self) -> int:
        return sum(1 for s in self.wallet_status.values() if s == OrderStatus.FILLED.value)

    def active_wallet_ids(self) -> list[str]:
        return [wallet_id for wallet_id, status in self.wallet_status.items() if status not in {OrderStatus.CANCELLED.value, OrderStatus.CLOSED.value, "settled"}]

    def is_within_force_close_window(self, now: datetime) -> bool:
        return (self.end_time - now).total_seconds() <= self.close_window_sec

    def remaining_to_start(self, now: datetime) -> float:
        return (self.start_time - now).total_seconds()

    def remaining_to_end(self, now: datetime) -> float:
        return (self.end_time - now).total_seconds()

    def should_halt(self) -> bool:
        return self.flow_state == EventFlowState.STOPPED or bool(self.halted_reason)


def build_wallet_identities(accounts: list[AccountContext]) -> list[WalletIdentity]:
    if len(accounts) < 2:
        raise ValueError("dual_wallet strategy requires at least two accounts")
    selected = accounts[:2]
    return [
        WalletIdentity(
            wallet_id=selected[0].account_id,
            wallet_name=selected[0].label,
            role=WalletRole.A,
            account=selected[0],
        ),
        WalletIdentity(
            wallet_id=selected[1].account_id,
            wallet_name=selected[1].label,
            role=WalletRole.B,
            account=selected[1],
        ),
    ]


def assign_event_sides(wallets: list[WalletIdentity]) -> tuple[dict[str, OrderSide], bool]:
    if len(wallets) != 2:
        raise ValueError("dual_wallet strategy requires exactly two wallets")
    first_wallet_is_up = bool(random.getrandbits(1))
    return {
        wallets[0].wallet_id: OrderSide.UP if first_wallet_is_up else OrderSide.DOWN,
        wallets[1].wallet_id: OrderSide.DOWN if first_wallet_is_up else OrderSide.UP,
    }, first_wallet_is_up


def format_operation_timestamp(ts: datetime | None = None) -> str:
    ts = ts or datetime.now()
    return ts.strftime("%m月%d日 %H:%M:%S")


def iter_wallet_orders(state: DualWalletEventState) -> Iterable[OrderSnapshot]:
    return state.wallet_orders.values()
