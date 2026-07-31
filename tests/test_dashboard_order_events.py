"""Tests for the dashboard's /api/order-events routes (Phase 2D)."""

from __future__ import annotations

import json

import pytest

from api import (
    OrderEvent,
    OrderEventBus,
    OrderStatus,
    reset_default_order_event_bus,
)


def _publish_some_events(bus: OrderEventBus) -> None:
    bus.publish(OrderEvent(
        event_id="",
        order_id="ct-abc-1",
        leader_wallet="0xabc",
        market_id="0xCOND",
        asset_id="0xASSET",
        side="BUY",
        status=OrderStatus.PENDING,
        reason="leader placed order",
        data={"price": 0.5, "size_usdc": 10.0},
    ))
    bus.publish(OrderEvent(
        event_id="",
        order_id="ct-abc-1",
        leader_wallet="0xabc",
        market_id="0xCOND",
        asset_id="0xASSET",
        side="BUY",
        status=OrderStatus.FILLED,
        data={"fill_price": 0.5},
    ))
    bus.publish(OrderEvent(
        event_id="",
        order_id="ct-abc-2",
        leader_wallet="0xdef",
        market_id="0xCOND",
        asset_id=None,
        side="SELL",
        status=OrderStatus.FAILED,
        reason="insufficient balance",
    ))


def _test_client(app):
    """Lazy import helper — TestClient lives in starlette.testclient."""
    from starlette.testclient import TestClient
    return TestClient(app)


def test_order_events_snapshot_route():
    reset_default_order_event_bus()
    bus = OrderEventBus()
    _publish_some_events(bus)
    # Replace the default singleton used by dashboard with our test bus.
    import api.order_events as oe
    oe._default_bus = bus

    from smart_money.dashboard_app import get_app
    app = get_app()
    client = _test_client(app)

    resp = client.get("/api/order-events?limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["stats"]["snapshot_size"] == 3
    statuses = [ev["status"] for ev in body["events"]]
    assert "pending" in statuses
    assert "filled" in statuses
    assert "failed" in statuses
    # Failed should appear in attention
    attention_statuses = {ev["status"] for ev in body["attention"]}
    assert "failed" in attention_statuses

    oe._default_bus = None
    reset_default_order_event_bus()


def test_order_events_snapshot_invalid_limit():
    reset_default_order_event_bus()
    from smart_money.dashboard_app import get_app
    app = get_app()
    client = _test_client(app)

    resp = client.get("/api/order-events?limit=0")
    assert resp.status_code == 400
    resp = client.get("/api/order-events?limit=99999")
    assert resp.status_code == 400
    reset_default_order_event_bus()


@pytest.mark.skip(reason="SSE streaming path is exercised manually via the dashboard UI; "
                   "blocking semantics on the test client hang the test suite.")
def test_order_events_stream_sends_hello_snapshot():
    reset_default_order_event_bus()
    bus = OrderEventBus()
    _publish_some_events(bus)
    import api.order_events as oe
    oe._default_bus = bus

    from smart_money.dashboard_app import get_app
    app = get_app()
    client = _test_client(app)

    # Use a short timeout so the read does not hang forever if the
    # async generator stops delivering chunks for any reason.
    resp = client.get("/api/order-events/stream", timeout=2.0)
    try:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        # Read the first chunk; the stream yields a "hello" payload
        # immediately on connect, so we only need the first read.
        first_chunk = next(resp.iter_text())
    finally:
        resp.close()
    assert "data:" in first_chunk
    line = [ln for ln in first_chunk.split("\n") if ln.startswith("data:")][0]
    payload = json.loads(line[len("data:"):].strip())
    assert payload["type"] == "hello"
    assert len(payload["snapshot"]) >= 3

    oe._default_bus = None
    reset_default_order_event_bus()


def test_routing_health_returns_layer_stats():
    from smart_money.dashboard_app import get_app
    app = get_app()
    client = _test_client(app)
    resp = client.get("/api/routing-health")
    assert resp.status_code == 200
    body = resp.json()
    assert "lanes" in body
    assert "breakers" in body
    assert "budgets" in body
    lanes = {lane["lane"]: lane for lane in body["lanes"]}
    assert "trade-write" in lanes
    assert "data-read" in lanes
    assert "warmup" in lanes