"""Tests for api.order_events — the copy-trade event bus."""

from __future__ import annotations

import threading

from api.order_events import (
    ATTENTION_STATUSES,
    OrderEvent,
    OrderEventBus,
    OrderStatus,
    new_order_id,
)


def _ev(order_id, status=OrderStatus.PENDING, **kwargs):
    return OrderEvent(
        event_id="",
        order_id=order_id,
        leader_wallet="0xabc",
        market_id="0xCOND",
        asset_id="0xASSET",
        side="BUY",
        status=status,
        reason=kwargs.pop("reason", None),
        data=kwargs.pop("data", {}),
    )


def test_new_order_id_format():
    oid = new_order_id("0xabcdef1234567890123456789012345678901234", "0xCOND12345")
    assert oid.startswith("ct-abcdef12")
    assert "0xCOND" in oid
    # uniqueness
    oid2 = new_order_id("0xabcdef1234567890123456789012345678901234", "0xCOND12345")
    assert oid != oid2


def test_publish_and_snapshot():
    bus = OrderEventBus()
    ev = _ev("o-1", OrderStatus.PENDING, reason="leader placed order")
    bus.publish(ev)
    snap = bus.snapshot()
    assert len(snap) == 1
    assert snap[0].order_id == "o-1"
    assert snap[0].status == OrderStatus.PENDING


def test_latest_by_order():
    bus = OrderEventBus()
    bus.publish(_ev("o-1", OrderStatus.PENDING))
    bus.publish(_ev("o-1", OrderStatus.INFLIGHT))
    bus.publish(_ev("o-1", OrderStatus.FILLED, data={"price": 0.5}))
    latest = bus.latest("o-1")
    assert latest is not None
    assert latest.status == OrderStatus.FILLED
    assert latest.data["price"] == 0.5


def test_subscribe_topic_filter_wallet():
    bus = OrderEventBus()
    received = []

    def cb(ev):
        received.append(ev)

    bus.subscribe(cb, topics=["wallet:0xabc"])
    bus.publish(_ev("o-1", OrderStatus.PENDING))
    bus.publish(OrderEvent(
        event_id="",
        order_id="o-2",
        leader_wallet="0xDEF",
        market_id="0xC",
        asset_id="0xA",
        side="SELL",
        status=OrderStatus.PENDING,
    ))
    assert len(received) == 1
    assert received[0].order_id == "o-1"


def test_subscribe_topic_filter_order_id():
    bus = OrderEventBus()
    received = []

    bus.subscribe(lambda ev: received.append(ev), topics=["order:o-1"])
    bus.publish(_ev("o-1", OrderStatus.PENDING))
    bus.publish(_ev("o-2", OrderStatus.PENDING))
    assert len(received) == 1


def test_subscribe_all_when_topics_none():
    bus = OrderEventBus()
    received = []
    bus.subscribe(lambda ev: received.append(ev))
    for i in range(3):
        bus.publish(_ev(f"o-{i}"))
    assert len(received) == 3


def test_unsubscribe_stops_delivery():
    bus = OrderEventBus()
    received = []
    sid = bus.subscribe(lambda ev: received.append(ev))
    bus.publish(_ev("o-1"))
    bus.unsubscribe(sid)
    bus.publish(_ev("o-2"))
    assert len(received) == 1


def test_attention_required():
    bus = OrderEventBus()
    bus.publish(_ev("o-1", OrderStatus.PENDING))
    bus.publish(_ev("o-2", OrderStatus.FAILED, reason="insufficient balance"))
    bus.publish(_ev("o-3", OrderStatus.PARTIAL, data={"filled": 0.3}))
    bus.publish(_ev("o-4", OrderStatus.FILLED))
    attn = bus.attention_required()
    statuses = {ev.order_id: ev.status for ev in attn}
    assert "o-2" in statuses
    assert "o-3" in statuses
    assert "o-1" not in statuses
    assert "o-4" not in statuses


def test_subscriber_exception_does_not_break_publisher():
    bus = OrderEventBus()
    received = []

    def bad_cb(ev):
        raise RuntimeError("boom")

    bus.subscribe(bad_cb)
    bus.subscribe(lambda ev: received.append(ev))
    bus.publish(_ev("o-1"))
    assert len(received) == 1


def test_thread_safe_concurrent_publish():
    bus = OrderEventBus()
    received = []
    lock = threading.Lock()

    def cb(ev):
        with lock:
            received.append(ev)

    bus.subscribe(cb)
    threads = [
        threading.Thread(
            target=lambda i=i: bus.publish(_ev(f"o-{i}", OrderStatus.INFLIGHT))
        )
        for i in range(20)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(received) == 20


def test_attention_statuses_constant():
    assert OrderStatus.FAILED in ATTENTION_STATUSES
    assert OrderStatus.PARTIAL in ATTENTION_STATUSES
    assert OrderStatus.SKIPPED in ATTENTION_STATUSES
    assert OrderStatus.FILLED not in ATTENTION_STATUSES
    assert OrderStatus.PENDING not in ATTENTION_STATUSES


def test_event_id_assigned_if_empty():
    bus = OrderEventBus()
    bus.publish(_ev("o-1"))
    snap = bus.snapshot()
    assert snap[0].event_id != ""