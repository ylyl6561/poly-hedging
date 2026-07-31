"""Tests for the Phase 2D copy-trade event hooks in smart_money.executor.

These tests focus on the bus publishing behaviour — the executor's
internal plan logic / DB writes are exercised by the existing
test_task_manager_integration suite.
"""

from __future__ import annotations

from types import SimpleNamespace

from api import (
    OrderEvent,
    OrderEventBus,
    OrderStatus,
)


class _FakeResult:
    def __init__(self, scalar):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class _FakeQuery:
    def filter(self, *_a, **_kw):
        return self
    def order_by(self, *_a, **_kw):
        return self
    def limit(self, *_a, **_kw):
        return self
    def all(self):
        return []
    def first(self):
        return None


class _FakeSession:
    def __init__(self, *, signal=None):
        self.added = []
        self._signal = signal

    def execute(self, stmt, *_a, **_kw):
        # Identify the "select(Signal).where(id == ...)" query — for our
        # tests it just needs to return the pre-canned signal so the
        # executor can move past the "missing signal" guard.
        return _FakeResult(self._signal)

    def query(self, *_a, **_kw):
        return _FakeQuery()

    def add(self, obj):
        self.added.append(obj)


def _settings():
    return SimpleNamespace(
        live_trade=False,
        follow_min_consensus_for_execute=3,
        follow_max_size_usdc=100.0,
        default_position_size_usdc=10.0,
        follow_clob_order_type="GTC",
        data_api_base="https://data-api.polymarket.com",
    )


def test_missing_signal_returns_without_bus_event():
    from smart_money.executor import execute

    bus = OrderEventBus()
    out = execute(_FakeSession(), _settings(), signal_id=999, bus=bus)
    assert out["status"] == "missing"
    # No event should be published when the signal itself doesn't exist
    assert bus.snapshot() == []


def test_dry_run_publishes_pending_and_mirrored():
    from smart_money.executor import _build_plan, execute

    # Build a plan manually to avoid DB plumbing.
    plan = SimpleNamespace(
        signal_id=1,
        wallet="0xABCDEF1234567890123456789012345678901234",
        condition_id="0xCONDITION",
        direction="YES",
        token_id="0xASSET",
        side="BUY",
        price=0.5,
        size_usdc=10.0,
        size_shares=20.0,
    )

    sig = SimpleNamespace(
        id=1,
        condition_id=plan.condition_id,
        direction=plan.direction,
        avg_entry_price=0.5,
        current_price=0.5,
        signal_type="consensus",
        trader_count=5,
        suggested_size_usdc=10.0,
        trigger_wallets=[{"wallet": plan.wallet}],
        trigger_trade_fingerprint=None,
    )

    # Patch _build_plan to return our pre-built plan.
    import smart_money.executor as ex
    original = ex._build_plan
    ex._build_plan = lambda *a, **kw: plan
    try:
        bus = OrderEventBus()
        out = execute(_FakeSession(signal=sig), _settings(), signal_id=1, bus=bus)
    finally:
        ex._build_plan = original

    assert out["status"] == "dry_run"
    snap = bus.snapshot()
    statuses = [ev.status for ev in snap]
    # Phase 2D: dry-run emits PENDING then MIRRORED.
    assert OrderStatus.PENDING in statuses
    assert OrderStatus.MIRRORED in statuses
    assert all(ev.order_id == out["order_id"] for ev in snap)
    # The first event (PENDING) should carry the plan data so the dashboard
    # can render the order intent immediately.
    pending = next(ev for ev in snap if ev.status == OrderStatus.PENDING)
    assert pending.data["price"] == 0.5
    assert pending.data["size_usdc"] == 10.0
    assert pending.leader_wallet == plan.wallet


def test_skipped_signal_publishes_skipped():
    import smart_money.executor as ex

    class _Sig:
        id = 7
        condition_id = "0xCOND"
        direction = "YES"
        avg_entry_price = 0.5
        current_price = 0.5
        signal_type = "consensus"
        trader_count = 1  # below threshold (3) → _build_plan returns None
        suggested_size_usdc = 10.0
        trigger_wallets = []
        trigger_trade_fingerprint = None

    bus = OrderEventBus()
    session = _FakeSession(signal=_Sig())
    out = ex.execute(session, _settings(), signal_id=7, bus=bus)
    assert out["status"] == "skipped"
    snap = bus.snapshot()
    assert any(ev.status == OrderStatus.SKIPPED for ev in snap)


def test_execute_uses_default_bus_when_none():
    import smart_money.executor as ex
    from api import reset_default_order_event_bus, get_order_event_bus

    reset_default_order_event_bus()
    plan = SimpleNamespace(
        signal_id=1,
        wallet="0xabcdef1234567890123456789012345678901234",
        condition_id="0xC",
        direction="YES",
        token_id="0xA",
        side="BUY",
        price=0.5,
        size_usdc=10.0,
        size_shares=20.0,
    )
    sig = SimpleNamespace(
        id=1,
        condition_id=plan.condition_id,
        direction=plan.direction,
        avg_entry_price=0.5,
        current_price=0.5,
        signal_type="consensus",
        trader_count=5,
        suggested_size_usdc=10.0,
        trigger_wallets=[{"wallet": plan.wallet}],
        trigger_trade_fingerprint=None,
    )
    original = ex._build_plan
    ex._build_plan = lambda *a, **kw: plan
    try:
        out = ex.execute(_FakeSession(signal=sig), _settings(), signal_id=1)
    finally:
        ex._build_plan = original

    # The default bus should have received the events.
    default_bus = get_order_event_bus()
    snap = default_bus.snapshot()
    assert any(ev.order_id == out["order_id"] for ev in snap)
    reset_default_order_event_bus()


def test_live_trade_publishes_pending_inflight_failed():
    import smart_money.executor as ex

    plan = SimpleNamespace(
        signal_id=1,
        wallet="0xabcdef1234567890123456789012345678901234",
        condition_id="0xC",
        direction="YES",
        token_id="0xA",
        side="BUY",
        price=0.5,
        size_usdc=10.0,
        size_shares=20.0,
    )
    sig = SimpleNamespace(
        id=1,
        condition_id=plan.condition_id,
        direction=plan.direction,
        avg_entry_price=0.5,
        current_price=0.5,
        signal_type="consensus",
        trader_count=5,
        suggested_size_usdc=10.0,
        trigger_wallets=[{"wallet": plan.wallet}],
        trigger_trade_fingerprint=None,
    )

    original_build = ex._build_plan
    original_submit = ex._submit_live
    ex._build_plan = lambda *a, **kw: plan
    ex._submit_live = lambda *a, **kw: (False, "clob submit failed: simulated")
    try:
        bus = OrderEventBus()
        settings = _settings()
        settings.live_trade = True
        out = ex.execute(_FakeSession(signal=sig), settings, signal_id=1, bus=bus)
    finally:
        ex._build_plan = original_build
        ex._submit_live = original_submit

    assert out["status"] == "error"
    statuses = [ev.status for ev in bus.snapshot()]
    # Live path emits PENDING -> INFLIGHT -> FAILED
    assert OrderStatus.PENDING in statuses
    assert OrderStatus.INFLIGHT in statuses
    assert OrderStatus.FAILED in statuses
    failed = next(ev for ev in bus.snapshot() if ev.status == OrderStatus.FAILED)
    assert "clob submit failed" in (failed.reason or "")