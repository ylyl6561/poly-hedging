"""
非阻塞轮询器模块 - Poller

提供各种轮询器的基类和实现，用于在状态机中非阻塞地检查条件是否满足。
每个轮询器都是非阻塞的：每次 poll() 只执行一次检查，返回当前状态。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Generic, TypeVar

from api import fetch_market_outcome, get_wallet_usdc_balance
from strategy.dual_wallet_models import EventOutcome


T = TypeVar("T")


@dataclass
class PollerResult(Generic[T]):
    """轮询结果。"""
    value: T | None = None
    is_complete: bool = False
    is_timeout: bool = False
    elapsed_sec: float = 0.0
    rounds: int = 0
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.is_complete and not self.is_timeout


class Poller(ABC):
    """
    非阻塞轮询器基类。

    每个轮询器：
    1. 在初始化时设置截止时间（deadline）
    2. 每次 poll() 执行一次检查
    3. 返回 PollerResult 包含当前状态
    4. 当 is_complete=True 时，轮询器完成
    """

    def __init__(
        self,
        name: str,
        timeout_sec: float,
        poll_interval_sec: float = 1.0,
        on_progress: callable | None = None,
        progress_interval_sec: float = 30.0,
    ):
        self.name = name
        self.timeout_sec = timeout_sec
        self.poll_interval_sec = poll_interval_sec
        self.on_progress = on_progress
        self.progress_interval_sec = progress_interval_sec

        self._deadline: float = 0.0
        self._start_time: float = 0.0
        self._last_poll_time: float = 0.0
        self._last_progress_time: float = 0.0
        self._rounds: int = 0
        self._is_started: bool = False
        self._is_complete: bool = False
        self._result: PollerResult = PollerResult()

    def start(self) -> None:
        """启动轮询器，设置截止时间。"""
        self._start_time = time.monotonic()
        self._deadline = self._start_time + self.timeout_sec
        self._last_poll_time = self._start_time
        self._last_progress_time = self._start_time
        self._rounds = 0
        self._is_started = True
        self._is_complete = False
        self._result = PollerResult()

    @property
    def is_complete(self) -> bool:
        """轮询器是否完成。"""
        return self._is_complete

    @property
    def result(self) -> PollerResult:
        """获取轮询结果。"""
        return self._result

    @property
    def elapsed_sec(self) -> float:
        """已用时间（秒）。"""
        return time.monotonic() - self._start_time if self._is_started else 0.0

    @property
    def remaining_sec(self) -> float:
        """剩余时间（秒）。"""
        return max(0, self._deadline - time.monotonic()) if self._is_started else self.timeout_sec

    def poll(self) -> PollerResult:
        """
        执行一次轮询（非阻塞）。

        返回 PollerResult，包含：
        - is_complete: 条件是否满足
        - is_timeout: 是否超时
        - value: 结果值（如果有）
        """
        if not self._is_started:
            self.start()

        self._rounds += 1
        self._last_poll_time = time.monotonic()
        elapsed = self.elapsed_sec
        remaining = self.remaining_sec

        # 检查是否超时
        if remaining <= 0:
            self._is_complete = True
            self._result = PollerResult(
                value=self._get_value(),
                is_complete=True,
                is_timeout=True,
                elapsed_sec=elapsed,
                rounds=self._rounds,
            )
            return self._result

        # 执行实际的检查
        check_result = self._check()
        self._result = PollerResult(
            value=check_result,
            is_complete=self._is_complete,
            is_timeout=False,
            elapsed_sec=elapsed,
            rounds=self._rounds,
        )

        # 触发进度回调
        if self.on_progress and (self._last_poll_time - self._last_progress_time >= self.progress_interval_sec):
            self._last_progress_time = self._last_poll_time
            self.on_progress(self)

        return self._result

    @abstractmethod
    def _check(self) -> Any:
        """执行实际检查逻辑。子类实现。"""
        ...

    def _get_value(self) -> Any:
        """获取结果值。子类可覆盖。"""
        return self._result.value

    def _complete(self, value: Any = None) -> None:
        """标记轮询器完成。"""
        self._is_complete = True
        if value is not None:
            self._result.value = value

    def get_status(self) -> dict:
        """获取状态摘要。"""
        return {
            "name": self.name,
            "is_complete": self._is_complete,
            "is_started": self._is_started,
            "elapsed_sec": round(self.elapsed_sec, 1),
            "remaining_sec": round(self.remaining_sec, 1),
            "rounds": self._rounds,
            "timeout_sec": self.timeout_sec,
        }


# ===== 具体的轮询器实现 =====


class OutcomePoller(Poller):
    """
    市场结果轮询器。

    检查 Polymarket 市场的结算结果。
    """

    def __init__(
        self,
        condition_id: str,
        timeout_sec: float = 900,
        poll_interval_sec: float = 5.0,
        on_progress: callable | None = None,
        progress_interval_sec: float = 30.0,
    ):
        super().__init__(
            name=f"市场结果轮询({condition_id[:8]}...)",
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
            on_progress=on_progress,
            progress_interval_sec=progress_interval_sec,
        )
        self.condition_id = condition_id
        self._last_payload: dict | None = None
        self._outcome: EventOutcome = EventOutcome.UNKNOWN

    def _check(self) -> EventOutcome:
        """检查市场结果。"""
        try:
            payload = fetch_market_outcome(self.condition_id, slug=None, clob_token_ids=None)
            if isinstance(payload, dict):
                self._last_payload = payload
                raw_outcome = str(
                    payload.get("outcome") or payload.get("winner") or ""
                ).upper()
                if raw_outcome in {"UP", "YES"}:
                    self._outcome = EventOutcome.UP
                    self._complete()
                elif raw_outcome in {"DOWN", "NO"}:
                    self._outcome = EventOutcome.DOWN
                    self._complete()
        except Exception as e:
            self._result.error = str(e)

        return self._outcome

    def _get_value(self) -> EventOutcome:
        return self._outcome

    def get_status(self) -> dict:
        base = super().get_status()
        base.update({
            "condition_id": self.condition_id,
            "outcome": self._outcome.value,
        })
        return base


class BalanceStabilityPoller(Poller):
    """
    余额稳定性轮询器。

    等待钱包余额连续多轮保持不变。
    """

    def __init__(
        self,
        wallet_balances_fn: callable,  # () -> dict[str, float | None]
        stable_rounds: int = 3,
        timeout_sec: float = 180,
        poll_interval_sec: float = 20.0,
        on_progress: callable | None = None,
        progress_interval_sec: float = 30.0,
    ):
        super().__init__(
            name="余额稳定性轮询",
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
            on_progress=on_progress,
            progress_interval_sec=progress_interval_sec,
        )
        self.wallet_balances_fn = wallet_balances_fn
        self.stable_rounds = stable_rounds
        self._last_balances: dict[str, float | None] | None = None
        self._current_balances: dict[str, float | None] | None = None
        self._stable_rounds: int = 0
        self._final_balances: dict[str, float | None] = {}

    def start(self) -> None:
        super().start()
        self._last_balances = None
        self._current_balances = None
        self._stable_rounds = 0
        self._final_balances = {}

    def _check(self) -> dict[str, float | None]:
        """检查余额是否稳定。"""
        try:
            self._current_balances = self.wallet_balances_fn()

            if self._last_balances is not None and self._balances_equal(
                self._last_balances, self._current_balances
            ):
                self._stable_rounds += 1
            else:
                self._stable_rounds = 1
                self._last_balances = dict(self._current_balances)

            if self._stable_rounds >= self.stable_rounds:
                self._final_balances = dict(self._current_balances)
                self._complete()

        except Exception as e:
            self._result.error = str(e)

        return self._current_balances or {}

    def _get_value(self) -> dict[str, float | None]:
        return self._final_balances or self._current_balances or {}

    @staticmethod
    def _balances_equal(
        left: dict[str, float | None] | None,
        right: dict[str, float | None] | None,
    ) -> bool:
        if left is None or right is None:
            return False
        keys = set(left) | set(right)
        for key in keys:
            left_val = left.get(key)
            right_val = right.get(key)
            if left_val is None or right_val is None:
                if left_val != right_val:
                    return False
                continue
            if abs(float(left_val) - float(right_val)) > 1e-6:
                return False
        return True

    def get_status(self) -> dict:
        base = super().get_status()
        base.update({
            "stable_rounds": self._stable_rounds,
            "required_rounds": self.stable_rounds,
            "current_balances": self._current_balances,
            "final_balances": self._final_balances,
        })
        return base


class OrderFillPoller(Poller):
    """
    订单成交轮询器。

    等待指定订单被成交。
    """

    def __init__(
        self,
        order_id: str,
        wallet_id: str,
        fetch_fn: callable,  # (order_id, wallet) -> ExecutionOutcome
        wallet: Any,
        timeout_sec: float = 120,
        poll_interval_sec: float = 1.0,
        on_progress: callable | None = None,
        progress_interval_sec: float = 30.0,
    ):
        super().__init__(
            name=f"订单成交轮询({order_id[:8]}...)",
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
            on_progress=on_progress,
            progress_interval_sec=progress_interval_sec,
        )
        self.order_id = order_id
        self.wallet_id = wallet_id
        self.fetch_fn = fetch_fn
        self.wallet = wallet
        self._is_filled: bool = False
        self._filled_shares: float = 0.0
        self._filled_amount_usd: float = 0.0

    def _check(self) -> dict:
        """检查订单状态。"""
        try:
            outcome = self.fetch_fn(self.order_id, wallet=self.wallet)
            if outcome.success:
                raw = outcome.raw if isinstance(outcome.raw, dict) else {}
                status = str(raw.get("status") or "").lower()
                if status == "filled":
                    self._is_filled = True
                    self._filled_shares = float(outcome.filled_shares or 0)
                    self._filled_amount_usd = float(outcome.filled_amount_usd or 0)
                    self._complete()
        except Exception as e:
            self._result.error = str(e)

        return {
            "is_filled": self._is_filled,
            "filled_shares": self._filled_shares,
            "filled_amount_usd": self._filled_amount_usd,
        }

    def _get_value(self) -> dict:
        return {
            "is_filled": self._is_filled,
            "filled_shares": self._filled_shares,
            "filled_amount_usd": self._filled_amount_usd,
        }

    def get_status(self) -> dict:
        base = super().get_status()
        base.update({
            "order_id": self.order_id,
            "wallet_id": self.wallet_id,
            "is_filled": self._is_filled,
            "filled_shares": self._filled_shares,
        })
        return base


# ===== 轮询器工厂 =====

def create_outcome_poller(
    condition_id: str,
    timeout_sec: float = 900,
    poll_interval_sec: float = 5.0,
    on_progress: callable | None = None,
) -> OutcomePoller:
    """创建市场结果轮询器。"""
    return OutcomePoller(
        condition_id=condition_id,
        timeout_sec=timeout_sec,
        poll_interval_sec=poll_interval_sec,
        on_progress=on_progress,
    )


def create_balance_poller(
    wallet_balances_fn: callable,
    stable_rounds: int = 3,
    timeout_sec: float = 180,
    poll_interval_sec: float = 20.0,
    on_progress: callable | None = None,
) -> BalanceStabilityPoller:
    """创建余额稳定性轮询器。"""
    return BalanceStabilityPoller(
        wallet_balances_fn=wallet_balances_fn,
        stable_rounds=stable_rounds,
        timeout_sec=timeout_sec,
        poll_interval_sec=poll_interval_sec,
        on_progress=on_progress,
    )
