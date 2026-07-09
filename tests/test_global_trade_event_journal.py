"""
Tests for GlobalTradeEventJournal
"""

import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from state.global_trade_event_journal import (
    GlobalTradeEventJournal,
    get_journal,
    JournalEventRecord,
)


@pytest.fixture
def temp_journal_dir():
    """Create a temporary directory for journal files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def journal(temp_journal_dir):
    """Create a fresh journal instance for each test."""
    # Reset singleton for testing
    GlobalTradeEventJournal._instance = None
    journal = GlobalTradeEventJournal(
        journal_dir=temp_journal_dir,
        filename="test_events.json",
    )
    yield journal
    # Clean up
    journal.flush()
    GlobalTradeEventJournal._instance = None


class TestGlobalTradeEventJournal:
    """Tests for GlobalTradeEventJournal class."""

    def test_journal_creates_file(self, temp_journal_dir, journal):
        """Test that journal creates the data file after flush."""
        # Add an event to make journal dirty
        journal.record_event(
            event_type="test",
            event_id="test-001",
        )
        # Flush to ensure file is written
        journal.flush()
        journal_path = temp_journal_dir / "test_events.json"
        assert journal_path.exists()

    def test_journal_loads_existing_data(self, temp_journal_dir, journal):
        """Test that journal loads existing data on init."""
        # Write some data
        journal.record_event(
            event_type="test_event",
            event_id="test-001",
            event_name="Test Event",
        )
        journal.flush()

        # Create new instance - should load existing data
        GlobalTradeEventJournal._instance = None
        new_journal = GlobalTradeEventJournal(
            journal_dir=temp_journal_dir,
            filename="test_events.json",
        )
        assert new_journal._data["total_events"] == 1

    def test_record_event(self, journal):
        """Test recording a basic event."""
        journal.record_event(
            event_type="test_event",
            event_id="test-001",
            event_name="Test Event",
            phase="PENDING",
            note="Test note",
        )

        events = journal._data["events"]
        assert len(events) == 1
        assert events[0]["event_type"] == "test_event"
        assert events[0]["event_id"] == "test-001"
        assert events[0]["event_name"] == "Test Event"
        assert events[0]["phase"] == "PENDING"

    def test_record_state_change(self, journal):
        """Test recording state changes."""
        journal.record_state_change(
            event_id="test-001",
            event_name="Test Event",
            from_state="PENDING",
            to_state="PLACING_ENTRY",
            wallet="Wallet A",
            wallet_id="w_a",
            side="UP",
            note="Starting order placement",
        )

        events = journal._data["events"]
        assert len(events) == 1
        assert events[0]["event_type"] == "state_change"
        assert events[0]["phase"] == "PLACING_ENTRY"
        assert events[0]["raw"]["from_state"] == "PENDING"
        assert events[0]["raw"]["to_state"] == "PLACING_ENTRY"

    def test_record_order(self, journal):
        """Test recording order events."""
        journal.record_order(
            event_id="test-001",
            event_name="Test Event",
            wallet="Wallet A",
            wallet_id="w_a",
            operation="PLACE",
            side="UP",
            order_id="order-123",
            token_id="token-456",
            amount_usd=10.0,
            price=0.5,
            shares=20.0,
            status="submitted",
            filled_shares=0.0,
        )

        orders = journal._data["orders"]
        assert len(orders) == 1
        assert orders[0]["event_type"] == "order"
        assert orders[0]["operation"] == "PLACE"
        assert orders[0]["side"] == "UP"
        assert orders[0]["order_id"] == "order-123"
        assert orders[0]["status"] == "submitted"

    def test_record_result(self, journal):
        """Test recording trade results."""
        journal.record_result(
            event_id="test-001",
            event_name="Test Event",
            outcome="UP",
            is_profit=True,
            profit_loss=5.25,
            pnl_percent=5.25,
            trigger_reason="both_sides_filled",
            wallet_results={"w_a": 3.0, "w_b": 2.25},
        )

        results = journal._data["results"]
        assert len(results) == 1
        assert results[0]["event_type"] == "result"
        assert results[0]["outcome"] == "UP"
        assert results[0]["is_profit"] is True
        assert results[0]["profit_loss"] == 5.25
        assert results[0]["raw"]["wallet_results"]["w_a"] == 3.0

        # Check summary
        summary = journal._data["summary"]
        assert summary["total_trades"] == 1
        assert summary["profitable_trades"] == 1
        assert summary["losing_trades"] == 0
        assert summary["total_pnl"] == 5.25

    def test_record_trade_start(self, journal):
        """Test recording trade start."""
        journal.record_trade_start(
            event_id="test-001",
            event_name="BTC 5m Test",
            condition_id="cond-123",
            start_time="2024-01-01T12:00:00+00:00",
            end_time="2024-01-01T12:05:00+00:00",
            wallet_assignments=[
                {"wallet_id": "w_a", "side": "UP"},
                {"wallet_id": "w_b", "side": "DOWN"},
            ],
        )

        events = journal._data["events"]
        assert len(events) == 1
        assert events[0]["event_type"] == "trade_start"
        assert events[0]["phase"] == "PENDING"
        assert events[0]["raw"]["condition_id"] == "cond-123"
        assert len(events[0]["raw"]["wallet_assignments"]) == 2

    def test_get_summary(self, journal):
        """Test getting journal summary."""
        journal.record_event(
            event_type="test",
            event_id="test-001",
        )
        journal.record_order(
            event_id="test-001",
            event_name="Test",
            wallet="W1",
            wallet_id="w1",
            operation="PLACE",
        )

        summary = journal.get_summary()
        assert summary["total_events"] == 1
        assert summary["total_orders"] == 1
        assert summary["total_results"] == 0

    def test_get_events_by_event_id(self, journal):
        """Test filtering events by event_id."""
        journal.record_event(event_type="test", event_id="test-001")
        journal.record_event(event_type="test", event_id="test-002")
        journal.record_state_change(
            event_id="test-001",
            event_name="Event 1",
            from_state="A",
            to_state="B",
        )
        journal.record_order(
            event_id="test-002",
            event_name="Event 2",
            wallet="W1",
            wallet_id="w1",
            operation="PLACE",
        )

        # test-001 has 2 events: one direct record + one from state_change
        result_001 = journal.get_events_by_event_id("test-001")
        assert len(result_001["events"]) == 2
        assert len(result_001["orders"]) == 0
        assert len(result_001["results"]) == 0

        # test-002 has 1 event + 1 order
        result_002 = journal.get_events_by_event_id("test-002")
        assert len(result_002["events"]) == 1
        assert len(result_002["orders"]) == 1
        assert len(result_002["results"]) == 0

    def test_auto_flush(self, journal):
        """Test that changes trigger auto-flush."""
        journal._dirty = False
        journal._changes_since_flush = 0

        # Record multiple events - should trigger flush
        for i in range(6):
            journal.record_event(
                event_type="test",
                event_id=f"test-{i:03d}",
            )

        # Verify events were recorded
        assert len(journal._data["events"]) == 6

    def test_context_manager(self, temp_journal_dir):
        """Test journal works as context manager."""
        GlobalTradeEventJournal._instance = None
        with GlobalTradeEventJournal(
            journal_dir=temp_journal_dir,
            filename="test_ctx.json",
        ) as journal:
            journal.record_event(
                event_type="test",
                event_id="test-ctx",
            )

        # Verify file exists with data
        journal_path = temp_journal_dir / "test_ctx.json"
        assert journal_path.exists()
        with open(journal_path) as f:
            data = json.load(f)
            assert data["total_events"] == 1

    def test_timestamp_fields(self, journal):
        """Test that timestamps are properly recorded."""
        before = datetime.now(timezone.utc)
        journal.record_event(
            event_type="test",
            event_id="test-ts",
        )
        after = datetime.now(timezone.utc)

        events = journal._data["events"]
        assert len(events) == 1

        # Parse timestamp
        ts = datetime.fromisoformat(events[0]["timestamp"].replace("Z", "+00:00"))
        assert before <= ts <= after

        # Check CN timestamp exists
        assert events[0]["timestamp_cn"] != ""


class TestGetJournal:
    """Tests for get_journal convenience function."""

    def test_get_journal_returns_instance(self):
        """Test get_journal returns GlobalTradeEventJournal instance."""
        GlobalTradeEventJournal._instance = None
        journal = get_journal()
        assert isinstance(journal, GlobalTradeEventJournal)

    def test_get_journal_same_instance(self):
        """Test get_journal returns singleton."""
        GlobalTradeEventJournal._instance = None
        journal1 = get_journal()
        journal2 = get_journal()
        assert journal1 is journal2


class TestJournalEventRecord:
    """Tests for JournalEventRecord dataclass."""

    def test_record_to_dict(self):
        """Test converting record to dict."""
        record = JournalEventRecord(
            timestamp="2024-01-01T12:00:00+00:00",
            timestamp_cn="2024-01-01 20:00:00",
            event_type="order",
            event_id="test-001",
            event_name="Test Event",
            phase="FILLED",
            wallet="Wallet A",
            wallet_id="w_a",
            side="UP",
            operation="FORCE_CLOSE",
            order_id="order-123",
            shares=10.0,
            filled_shares=10.0,
            status="filled",
            is_profit=True,
            profit_loss=5.0,
        )

        d = record.to_dict()
        assert d["event_type"] == "order"
        assert d["wallet"] == "Wallet A"
        assert d["operation"] == "FORCE_CLOSE"
        assert d["is_profit"] is True
        assert d["profit_loss"] == 5.0
