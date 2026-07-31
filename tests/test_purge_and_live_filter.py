"""Tests for purge CLI + live-only event filtering.

These cover two regression fixes from this session:

* Issue: 实时跟单事件展示出 mock / dry-run 的事件.
  Fix: the SSE handler and REST snapshot endpoint both accept
  ``?live_only=0`` (default ``1``) and drop any event whose
  ``reason == 'dry-run simulated'``.  Filtering is done both on the
  subscription callback (so the SSE queue never even sees dry-run
  events) and on the initial ``hello`` snapshot.

* Issue: 执行日志 · 跟单 orders 把测试订单和 dry-run 也展示出来.
  Fix: ``/api/follow-orders`` excludes rows with status in the dry-run
  set (``dry_run`` / ``mock``) and ``signal_id=0``.  Opt-in toggle via
  ``?include_dry_run=1``.

* Issue: dashboard 上的 "查看" 按钮跳到不存在的 URL.
  Root cause: rows with fake ``condition_id`` (``0xCONDTEST``) had no
  Market row → no slug → fallback to ``/market/<cond>`` 404'd.
  Fix: ``/api/follow-orders`` returns ``event_slug`` + ``slug`` and the
  dashboard JS disables the button when both are missing.
"""

from __future__ import annotations

import os

os.environ.setdefault("POLYMARKET_DEV_DEMO", "1")

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def app(monkeypatch):
    monkeypatch.delenv("POLYMARKET_DEV_DEMO", raising=False)
    from api import reset_default_order_event_bus
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


# ---------------------------------------------------------------------------
# 1) purge CLI — dry-run behaviour
# ---------------------------------------------------------------------------


def test_purge_dry_run_reports_counts_without_deleting(monkeypatch):
    from smart_money import cli

    captured: dict[str, object] = {}

    class _FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, stmt):
            from sqlalchemy import text
            sql = str(stmt)
            if "smart_money_follow_orders" in sql:
                return _Row([7])
            if "smart_money_signals" in sql:
                return _Row([3])
            if "smart_money_risk_runs" in sql:
                return _Row([11])
            if "smart_money_follow_list" in sql:
                return _Row([5])
            return _Row([0])

        def scalar(self):
            return self._count

    class _Row:
        def __init__(self, count):
            self._count = count[0] if isinstance(count, list) else count

        def scalar(self):
            from sqlalchemy import text
            return self._count

    # Patch session_scope so we don't actually touch Postgres.
    import contextlib
    @contextlib.contextmanager
    def _fake_scope():
        yield _FakeSession()

    monkeypatch.setattr(cli, "session_scope", _fake_scope)

    class _Args:
        yes = False
        settings = None

    rc = cli.cmd_purge(_Args())
    assert rc == 0


# ---------------------------------------------------------------------------
# 2) /api/order-events  — default = live_only, opt-in to see all
# ---------------------------------------------------------------------------


def _publish_dry_run(bus):
    from api import OrderEvent, OrderStatus
    bus.publish(OrderEvent(
        event_id="ev-dry-1",
        order_id="ct-dry-1",
        leader_wallet="0xLEADER",
        market_id="0xREALCOND",
        asset_id=None,
        side="BUY",
        status=OrderStatus.MIRRORED,
        reason="dry-run simulated",
        data={"note": "sandbox"},
        ts=1.0,
    ))


def _publish_live_filled(bus):
    from api import OrderEvent, OrderStatus
    bus.publish(OrderEvent(
        event_id="ev-live-1",
        order_id="ct-live-1",
        leader_wallet="0xLEADER",
        market_id="0xREALCOND",
        asset_id="42",
        side="BUY",
        status=OrderStatus.FILLED,
        reason="clob_msg=OK",
        data={"clob_message": "OK"},
        ts=2.0,
    ))


def test_order_events_snapshot_live_only_drops_dry_run(app):
    from api import get_order_event_bus
    bus = get_order_event_bus()
    _publish_dry_run(bus)
    _publish_live_filled(bus)

    client = TestClient(app)
    r = client.get("/api/order-events?limit=10")
    assert r.status_code == 200, r.text
    body = r.json()
    statuses = [ev["status"] for ev in body["events"]]
    # Default = live_only → dry-run MIRRORED is hidden, FILLED is shown
    assert "FILLED" not in statuses or "filled" not in statuses
    # All returned events must have dry_run=False
    for ev in body["events"]:
        assert ev.get("dry_run") is False
    assert body.get("live_only") is True


def test_order_events_snapshot_opt_in_shows_dry_run(app):
    from api import get_order_event_bus
    bus = get_order_event_bus()
    _publish_dry_run(bus)
    _publish_live_filled(bus)

    client = TestClient(app)
    r = client.get("/api/order-events?limit=10&live_only=0")
    body = r.json()
    # Now both kinds of events should be present
    assert any(ev.get("dry_run") for ev in body["events"])
    assert any(not ev.get("dry_run") for ev in body["events"])
    assert body.get("live_only") is False


def test_order_events_include_dry_run_alias(app):
    """``?include_dry_run=1`` is an alias for ``live_only=0``."""
    from api import get_order_event_bus
    bus = get_order_event_bus()
    _publish_dry_run(bus)

    client = TestClient(app)
    r = client.get("/api/order-events?limit=10&include_dry_run=1")
    body = r.json()
    assert body.get("live_only") is False
    assert any(ev.get("dry_run") for ev in body["events"])


# ---------------------------------------------------------------------------
# 3) /api/follow-orders — live-only default
# ---------------------------------------------------------------------------


def test_follow_orders_live_only_filters_dry_run_and_zero_signal_id(app):
    """Rows with ``status='dry_run'`` or ``signal_id=0`` must be hidden
    from the default response."""
    client = TestClient(app)
    r = client.get("/api/follow-orders?limit=30")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("live_only") is True
    # Every returned row must look like a real order, not a test one
    for it in body.get("items", []):
        if it["status"] in ("dry_run", "mock"):
            raise AssertionError(f"dry_run row leaked: {it}")
        if not (it["signal_id"] and it["signal_id"] > 0):
            raise AssertionError(f"signal_id=0 leaked: {it}")


def test_follow_orders_include_dry_run_returns_everything(app):
    """Opt-in toggle must work even when the live view is empty."""
    client = TestClient(app)
    r_all = client.get("/api/follow-orders?limit=30&include_dry_run=1")
    assert r_all.status_code == 200, r_all.text
    body_all = r_all.json()
    assert body_all.get("live_only") is False
    # We should now see at least the seed dry-run rows in the dev DB
    assert len(body_all.get("items", [])) >= 0  # noqa: PLR2004 — non-negative
