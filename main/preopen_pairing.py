"""Preopen paired-leg generation utilities.

This module is intentionally pure: it does not place orders. It only describes
which paired legs should be attempted for a given primary action.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PairingPhase(str, Enum):
    UP_ENTRY = "up_entry"
    DOWN_RESTING = "down_resting"


class OrderIntent(str, Enum):
    MARKET_LIKE = "market_like"      # immediate-style (uses order type like the UP entry)
    LIMIT_RESTING = "limit_resting"  # post-only resting limit


@dataclass(frozen=True)
class PlannedLeg:
    """A leg the executor may attempt to place.

    shares: number of outcome shares.
    price_cap: for MARKET_LIKE orders, the worst acceptable price (0..1).
               for LIMIT_RESTING orders, the exact limit price.
    """

    side: str  # "yes" or "no"
    intent: OrderIntent
    shares: float
    price_cap: float
    post_only: bool = False
    order_type: str | None = None  # e.g. "GTC", "FAK" (optional override)


def build_paired_legs(
    *,
    phase: PairingPhase,
    shares: float,
    up_price_cap: float,
    down_price_cap: float,
    up_resting_price: float,
    down_resting_price: float,
) -> list[PlannedLeg]:
    """Build paired legs for the given phase.

    Rules required by the user:
    - When placing UP (YES) entry, also place corresponding DOWN (NO) as non-resting.
    - When placing DOWN (NO) resting, also place corresponding UP (YES) resting.

    We keep this logic declarative and strategy-agnostic; caller decides how
    to map MARKET_LIKE to concrete order types.
    """

    if shares <= 0:
        return []

    if phase == PairingPhase.UP_ENTRY:
        # Primary leg is UP entry; paired leg is DOWN entry (market-like)
        return [
            PlannedLeg(
                side="no",
                intent=OrderIntent.MARKET_LIKE,
                shares=float(shares),
                price_cap=float(down_price_cap),
                post_only=False,
            )
        ]

    if phase == PairingPhase.DOWN_RESTING:
        # Primary leg is DOWN resting; paired leg is UP resting
        return [
            PlannedLeg(
                side="yes",
                intent=OrderIntent.LIMIT_RESTING,
                shares=float(shares),
                price_cap=float(up_resting_price),
                post_only=True,
                order_type="GTC",
            )
        ]

    return []
