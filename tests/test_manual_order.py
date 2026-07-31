"""Tests for the Phase 4 manual-order + leader-sales endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from api import (
    OrderEvent,
    OrderEventBus,
    OrderStatus,
    reset_default_order_event_bus,
)


def _test_client(app):
    from starlette.testclient import TestClient
    return TestClient(app)


@pytest.fixture
def app(monkeypatch):
    """Build the dashboard app with an isolated bus."""
    monkeypatch.delenv("POLYMARKET_DEV_DEMO", raising=False)
    reset_default_order_event_bus()
    from smart_money.dashboard_app import get_app  # noqa: E402
    return get_app()


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("SMART_MONEY_LIVE_TRADE", "0")
    from smart_money.config import get_settings  # noqa: E402
    get_settings.cache_clear()
    s = get_settings()
    yield s
    get_settings.cache_clear()


def test_manual_order_rejects_bad_payloads(app, settings):
    client = _test_client(app)
    # missing condition_id → 400
    r = client.post(
        "/api/follow/manual-order",
        json={"side": "BUY", "price": 0.4, "size_usdc": 5},
    )
    assert r.status_code == 400, r.text

    # non-numeric price → 400
    r = client.post(
        "/api/follow/manual-order",
        json={"condition_id": "0xCOND", "side": "BUY", "price": "oops", "size_usdc": 5},
    )
    assert r.status_code == 400, r.text

    # Business-rule rejections return 200 with status=skipped
    # bad price
    r = client.post(
        "/api/follow/manual-order",
        json={"condition_id": "0xCOND", "side": "BUY", "price": 1.5, "size_usdc": 5},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "skipped"
    assert "price" in body["reason"].lower()

    # size too small
    r = client.post(
        "/api/follow/manual-order",
        json={"condition_id": "0xCOND", "side": "BUY", "price": 0.4, "size_usdc": 0.5},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "skipped"


def test_manual_order_dry_run_publishes_events(app, settings):
    """End-to-end: posting a manual order during DRY-RUN should emit
    PENDING + MIRRORED OrderEvents that hit the process-wide bus."""
    client = _test_client(app)
    from api import get_order_event_bus
    bus = get_order_event_bus()
    received: list[OrderEvent] = []
    bus.subscribe(received.append)

    payload = {
        "condition_id": "0xCONDTEST",
        "side": "BUY",
        "price": 0.4,
        "size_usdc": 5,
        "direction": "YES",
        "leader_wallet": "manual-test",
    }
    r = client.post("/api/follow/manual-order", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "dry_run", body
    assert "order_id" in body and body["order_id"].startswith("ct-")
    statuses = [ev.status for ev in received if ev.order_id == body["order_id"]]
    assert OrderStatus.PENDING in statuses
    assert OrderStatus.MIRRORED in statuses


def test_detect_sales_requires_lookback(app, settings):
    client = _test_client(app)
    r = client.post("/api/follow/detect-sales", json={"lookback_minutes": 9999})
    assert r.status_code == 400
    r = client.post("/api/follow/detect-sales", json={"lookback_minutes": -1})
    assert r.status_code == 400


def test_detect_sales_skips_when_follow_list_empty(app, settings):
    client = _test_client(app)
    r = client.post("/api/follow/detect-sales", json={"lookback_minutes": 30})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 0
    assert body.get("skipped") is True or body.get("sales") == []


def test_detect_sales_emits_partial_when_leader_exits(app, settings, monkeypatch):
    """Stub the DB path so we don't need Postgres-only DDL on SQLite.

    The function under test reads three result sets in order:
      1. ``select(FollowListEntry.wallet, .username)``
      2. ``select(*).where(...).all()`` for held ``FollowOrder`` rows
      3. ``select(Trade)`` rows with ``.scalars().all()``
    We feed those into a tiny stub session.
    """
    from smart_money import manual_order as mo_mod  # noqa: E402
    from smart_money.config import get_settings  # noqa: E402

    monkeypatch.setenv("SMART_MONEY_FEISHU_WEBHOOK_URL", "")
    get_settings.cache_clear()

    follow_wallet = "0xleader"
    condition_id = "0xCOND"
    leader_order_id = 10

    class _Row:
        def __init__(self, **kw):
            self.__dict__.update(kw)

        def __getitem__(self, k):
            return self.__dict__[k]

    class _HeldRow(tuple):
        def __new__(cls, **kw):
            return super().__new__(cls, (
                kw["condition_id"], kw["wallet"], kw["token_id"],
                kw["price"], kw["size_usdc"], kw["id"], kw["direction"],
            ))

        def __getattr__(self, name):
            for attr, idx in (
                ("condition_id", 0), ("wallet", 1), ("token_id", 2),
                ("price", 3), ("size_usdc", 4), ("id", 5), ("direction", 6),
            ):
                if attr == name:
                    return self[idx]
            raise AttributeError(name)

    held_rows = [
        _HeldRow(
            condition_id=condition_id, wallet=follow_wallet,
            token_id="0xTOKEN", price=0.4, size_usdc=50.0,
            id=leader_order_id, direction="YES",
        ),
    ]
    now = datetime.now(timezone.utc)
    trade_rows = [
        _Row(
            wallet=follow_wallet, condition_id=condition_id, token_id="0xTOKEN",
            side="SELL", price=0.55, size=10, amount=5.5,
            traded_at=now - timedelta(minutes=2),
            title="Will BTC > 100k?", slug="btc-100k",
            fingerprint="t-1",
        ),
    ]

    class _StubResult:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return list(self._rows)

        def scalars(self):
            rows = list(self._rows)

            class _S:
                def all(self_inner):
                    return rows

            return _S()

        def first(self):
            return self._rows[0] if self._rows else None

        def scalar_one_or_none(self):
            return self._rows[0] if self._rows else None

    class _StubSession:
        def __init__(self):
            self._executed: list[int] = []
            self._queues = [
                [(follow_wallet, "L")],   # follow list
                held_rows,                # FollowOrder open positions
                trade_rows,               # recent Trade rows
            ]

        def execute(self, stmt):
            idx = min(len(self._executed), len(self._queues) - 1)
            self._executed.append(1)
            return _StubResult(self._queues[idx])

    stub = _StubSession()
    bus = OrderEventBus()
    received: list[OrderEvent] = []
    bus.subscribe(received.append)

    cfg = get_settings()
    result = mo_mod.detect_leader_sales(
        stub,  # type: ignore[arg-type]
        cfg,
        lookback_minutes=30,
        bus=bus,
    )

    assert result["count"] == 1
    assert result["sales"][0]["leader_wallet"] == follow_wallet
    assert result["sales"][0]["matched_held_order"] == leader_order_id
    assert any(ev.status == OrderStatus.PARTIAL for ev in received)
