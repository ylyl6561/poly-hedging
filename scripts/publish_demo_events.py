"""Push simulated order events through the OrderEventBus so we can
verify the dashboard's SSE pipeline end-to-end."""

import asyncio
import os
import sys

# Make sure the dashboard uses the *same* bus singleton as our publisher.
# The dashboard's uvicorn worker already has its own _default_bus;
# we re-publish by hitting the in-process module the dashboard loaded.
#
# Strategy: run a tiny uvicorn-loaded publisher inside the same Python
# process as the dashboard. We do that by importing the same module the
# dashboard imported and calling publish() on its singleton.

import time

from api import (
    OrderEvent,
    OrderEventBus,
    OrderStatus,
    get_order_event_bus,
)


def main():
    bus = get_order_event_bus()
    leader_wallet = "0xabcdef1234567890123456789012345678901234"
    market_a = "0xCONDAAA"
    market_b = "0xCONDBBB"

    # 1) First copy-trade — PENDING then MIRRORED (dry-run happy path)
    oid1 = "ct-demo01-aaa-11111111"
    print(f"publishing PENDING for {oid1}")
    bus.publish(OrderEvent(
        event_id="",
        order_id=oid1,
        leader_wallet=leader_wallet,
        market_id=market_a,
        asset_id="0xASSET111",
        side="BUY",
        status=OrderStatus.PENDING,
        reason="leader placed order",
        data={"price": 0.42, "size_usdc": 50.0, "size_shares": 119.05},
    ))

    time.sleep(1.0)

    print(f"publishing MIRRORED for {oid1}")
    bus.publish(OrderEvent(
        event_id="",
        order_id=oid1,
        leader_wallet=leader_wallet,
        market_id=market_a,
        asset_id="0xASSET111",
        side="BUY",
        status=OrderStatus.MIRRORED,
        reason="dry-run simulated",
        data={"note": "DRY-RUN side=BUY price=0.42 size=$50.0 (119.05 shares)"},
    ))

    # 2) Second copy-trade — live path with FILLED
    time.sleep(1.5)
    oid2 = "ct-demo02-bbb-22222222"
    print(f"publishing PENDING/INFLIGHT/FILLED for {oid2}")
    bus.publish(OrderEvent(
        event_id="",
        order_id=oid2,
        leader_wallet=leader_wallet,
        market_id=market_b,
        asset_id="0xASSET222",
        side="BUY",
        status=OrderStatus.PENDING,
        reason="leader placed order",
        data={"price": 0.65, "size_usdc": 30.0},
    ))
    time.sleep(0.8)
    bus.publish(OrderEvent(
        event_id="",
        order_id=oid2,
        leader_wallet=leader_wallet,
        market_id=market_b,
        asset_id="0xASSET222",
        side="BUY",
        status=OrderStatus.INFLIGHT,
        reason="submitting to CLOB",
    ))
    time.sleep(0.8)
    bus.publish(OrderEvent(
        event_id="",
        order_id=oid2,
        leader_wallet=leader_wallet,
        market_id=market_b,
        asset_id="0xASSET222",
        side="BUY",
        status=OrderStatus.FILLED,
        data={"fill_price": 0.65, "shares": 46.15},
    ))

    # 3) Third copy-trade — failed live
    time.sleep(1.5)
    oid3 = "ct-demo03-ccc-33333333"
    print(f"publishing PENDING/INFLIGHT/FAILED for {oid3}")
    bus.publish(OrderEvent(
        event_id="",
        order_id=oid3,
        leader_wallet="0xdeadbeef00000000000000000000000000009999",
        market_id=market_b,
        asset_id="0xASSET333",
        side="SELL",
        status=OrderStatus.PENDING,
        reason="leader closed position",
        data={"price": 0.71},
    ))
    time.sleep(0.5)
    bus.publish(OrderEvent(
        event_id="",
        order_id=oid3,
        leader_wallet="0xdeadbeef00000000000000000000000000009999",
        market_id=market_b,
        asset_id="0xASSET333",
        side="SELL",
        status=OrderStatus.INFLIGHT,
        reason="submitting to CLOB",
    ))
    time.sleep(0.8)
    bus.publish(OrderEvent(
        event_id="",
        order_id=oid3,
        leader_wallet="0xdeadbeef00000000000000000000000000009999",
        market_id=market_b,
        asset_id="0xASSET333",
        side="SELL",
        status=OrderStatus.FAILED,
        reason="clob submit failed: insufficient balance",
        data={"clob_message": "OrderArgs validation rejected: size < minimum"},
    ))

    # 4) Fourth — skipped (consensus dropped)
    time.sleep(1.5)
    oid4 = "ct-demo04-ddd-44444444"
    print(f"publishing SKIPPED for {oid4}")
    bus.publish(OrderEvent(
        event_id="",
        order_id=oid4,
        leader_wallet="",
        market_id="0xCONDDDD",
        asset_id=None,
        side=None,
        status=OrderStatus.SKIPPED,
        reason="plan-build rejected (price band / consensus / no token)",
        data={"signal_id": 7, "direction": "YES"},
    ))

    print(f"done publishing; bus stats = {bus.stats()}")


if __name__ == "__main__":
    main()
