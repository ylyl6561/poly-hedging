"""Event-level budget estimation and preflight checks for preopen trading.

Goal: prevent thrashing when wallet USDC is insufficient. We compute a conservative
upper bound of USDC that could be reserved/spent by the event's planned actions.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BudgetCheckResult:
    ok: bool
    required_usdc: float
    available_usdc: float
    reason: str | None = None


def estimate_event_budget_usdc(
    *,
    yes_shares: float,
    yes_price_cap: float,
    down_entry_shares: float,
    down_entry_price_cap: float,
    down_resting_shares: float,
    down_resting_price: float,
    up_resting_shares: float,
    up_resting_price: float,
    buffer_usdc: float = 0.0,
) -> float:
    """Conservative required USDC for all potential legs of an event.

    We intentionally over-estimate: sum of caps * shares for each leg.
    """

    required = 0.0

    def _add(shares: float, price: float):
        nonlocal required
        if shares and shares > 0 and price and price > 0:
            required += float(shares) * float(price)

    _add(yes_shares, yes_price_cap)
    _add(down_entry_shares, down_entry_price_cap)
    _add(down_resting_shares, down_resting_price)
    _add(up_resting_shares, up_resting_price)

    required += float(buffer_usdc or 0.0)
    return required


def check_budget(
    *,
    available_usdc: float | None,
    required_usdc: float,
    min_free_usdc: float = 0.0,
) -> BudgetCheckResult:
    """Return ok iff available_usdc >= required_usdc + min_free_usdc."""

    if available_usdc is None:
        return BudgetCheckResult(
            ok=False,
            required_usdc=float(required_usdc),
            available_usdc=0.0,
            reason="missing_balance_usdc",
        )

    try:
        avail = float(available_usdc)
    except (TypeError, ValueError):
        return BudgetCheckResult(
            ok=False,
            required_usdc=float(required_usdc),
            available_usdc=0.0,
            reason="invalid_balance_usdc",
        )

    need = float(required_usdc) + float(min_free_usdc or 0.0)
    if avail + 1e-9 < need:
        return BudgetCheckResult(
            ok=False,
            required_usdc=float(required_usdc),
            available_usdc=avail,
            reason=f"insufficient_balance_usdc:need>={need:.2f}",
        )

    return BudgetCheckResult(ok=True, required_usdc=float(required_usdc), available_usdc=avail, reason=None)
