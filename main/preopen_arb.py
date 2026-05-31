"""
Arbitrage opportunity gate for pre-open YES+Down strategy.

Evaluates whether the Down (NO) hedge is worth maintaining as a BTC-price hedge.
The hedge works as a directional protection (short YES exposure when BTC drops),
not as an intra-market arb (both legs of a binary always have negative edge after fees).

Gate: switch from GTC@0.40 to FAK if:
  1. ttl <= 40s (time pressure)
  2. Down price is still executable at a reasonable level
     -> net_edge = YES_fill_price - (YES_cost + NO_cost) > min_arb_edge
     (The edge measures how far below YES_price the NO_cost sits, scaled by the hedge)

Note: net_edge will be negative for most price pairs due to double fees.
The gate uses a very small min_arb_edge (default 0.01) as a sanity floor,
combined with the direct price cap check (NO < down_fak_max_price = 0.42).
"""

from dataclasses import dataclass


@dataclass
class ArbEdgeResult:
    yes_fill_price: float       # Assumed / estimated YES fill price
    down_executable_price: float  # Best available Down price (ask)
    yes_cost: float            # YES cost including fee
    down_cost: float           # Down cost including fee
    net_edge: float            # YES_price - (YES_cost + Down_cost)
    sufficient: bool           # net_edge >= min_arb_edge
    reason: str                # Human-readable reason


def evaluate_arb_edge(
    yes_fill_price: float,
    yes_side_ask: float,
    no_side_ask: float,
    fee_rate_bps: int,
    min_arb_edge: float,
) -> ArbEdgeResult:
    """
    Evaluate whether the Down hedge is worth maintaining.

    The hedge is a directional protection, not an intra-market arb:
      - Long YES on Polymarket (bullish signal confirmed by BTC momentum)
      - Long NO as a hedge (if BTC drops, YES falls, NO rises)
      - The hedge cost is the NO leg; the "edge" is how much below 1.0 the total cost sits

    net_edge = YES_fill_price - (YES_cost + NO_cost)
    (This is negative when double-fee costs exceed YES_fill_price)

    The gate passes when net_edge >= min_arb_edge (a tiny floor)
    OR when the NO price is low enough that the hedge isn't dead-money.

    For typical values (YES=0.92, NO=0.40, fee=1%):
      YES_cost = 0.92 * 1.01 = 0.9292
      NO_cost  = 0.40 * 1.01 = 0.4040
      net_edge = 0.92 - 1.3332 = -0.4132  ← always negative

    The real gate is the price cap: NO must stay <= down_fak_max_price (0.42).
    We add net_edge as a secondary sanity check.

    Args:
        yes_fill_price: The price we paid for YES
        yes_side_ask: Current best ask for YES side
        no_side_ask: Current best ask for NO side (this is our Down hedge)
        fee_rate_bps: Polymarket fee in basis points
        min_arb_edge: Minimum required edge (typically very small, e.g. 0.01)

    Returns:
        ArbEdgeResult with edge calculation
    """
    fee_rate = fee_rate_bps / 10_000.0

    # YES cost = fill price * (1 + fee_rate)
    yes_cost = yes_fill_price * (1.0 + fee_rate)

    # Down (NO) cost = best NO ask * (1 + fee_rate)
    down_cost = no_side_ask * (1.0 + fee_rate)

    # Net edge: positive when YES_price > total_cost
    # This will often be negative due to double fees on both legs
    net_edge = yes_fill_price - (yes_cost + down_cost)

    sufficient = net_edge >= min_arb_edge

    if sufficient:
        reason = (
            f"edge={net_edge:.4f} >= min={min_arb_edge:.4f}; "
            f"YES@{yes_fill_price:.3f} cost={yes_cost:.4f} + NO@{no_side_ask:.3f} cost={down_cost:.4f}"
        )
    else:
        reason = (
            f"edge={net_edge:.4f} < min={min_arb_edge:.4f}; "
            f"YES@{yes_fill_price:.3f} cost={yes_cost:.4f} + NO@{no_side_ask:.3f} cost={down_cost:.4f}"
        )

    return ArbEdgeResult(
        yes_fill_price=yes_fill_price,
        down_executable_price=no_side_ask,
        yes_cost=yes_cost,
        down_cost=down_cost,
        net_edge=net_edge,
        sufficient=sufficient,
        reason=reason,
    )


def check_arb_from_orderbook(
    yes_fill_price: float,
    clob_token_ids: list[str],
    fee_rate_bps: int,
    min_arb_edge: float,
    fetch_side_orderbook_price,
) -> ArbEdgeResult | None:
    """
    Fetch the orderbook and evaluate arbitrage edge.

    Args:
        yes_fill_price: Our YES fill price
        clob_token_ids: [yes_token, no_token]
        fee_rate_bps: Polymarket fee in bps
        min_arb_edge: Minimum required net edge
        fetch_side_orderbook_price: Function from api module

    Returns:
        ArbEdgeResult or None on error
    """
    if not clob_token_ids or len(clob_token_ids) < 2:
        return None

    yes_side = fetch_side_orderbook_price(clob_token_ids, "yes")
    no_side = fetch_side_orderbook_price(clob_token_ids, "no")

    if no_side is None:
        return None

    no_ask = no_side.get("best_ask")
    if no_ask is None or no_ask <= 0:
        return None

    # For YES ask, use the fill price as reference (we already filled)
    yes_ask = yes_side.get("best_ask") if yes_side else yes_fill_price

    return evaluate_arb_edge(
        yes_fill_price=yes_fill_price,
        yes_side_ask=yes_ask,
        no_side_ask=no_ask,
        fee_rate_bps=fee_rate_bps,
        min_arb_edge=min_arb_edge,
    )
