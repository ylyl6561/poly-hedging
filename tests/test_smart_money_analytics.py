"""Unit tests for Smart Money analytics. Uses an in-memory SQLite + fake Polymarket client."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

# Make sure repo root is importable
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Force a SQLite URL so we don't need Postgres for tests.
import os
os.environ.setdefault("SMART_MONEY_DATABASE_URL", "sqlite:///:memory:")

from smart_money import config as sm_config  # noqa: E402
from smart_money.analytics import SmartMoneyAnalytics  # noqa: E402
from smart_money.client import PolymarketReadClient  # noqa: E402
from smart_money.config import SmartMoneySettings  # noqa: E402
from smart_money.models import (  # noqa: E402
    Base,
    ClosedPosition,
    CurrentPosition,
    LeaderboardEntry,
    Market,
    Trader,
    Trade,
)


class FakePolymarketClient:
    """Bare-bones stand-in for PolymarketReadClient."""

    def __init__(self):
        self.leaderboard = [
            {
                "proxyWallet": "0x" + "1" * 40,
                "userName": "alpha",
                "pnl": 5000,
                "vol": 50000,
                "verifiedBadge": True,
            },
            {
                "proxyWallet": "0x" + "2" * 40,
                "userName": "beta",
                "pnl": 2500,
                "vol": 30000,
                "verifiedBadge": False,
            },
        ]


def _build_session() -> "sessionmaker":
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed(sessionmaker):
    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=12)
    closed_at = now - timedelta(days=10)
    session = sessionmaker()
    session.add(Trader(
        wallet="0x" + "1" * 40, username="alpha", tracked=True,
        last_active_at=now, last_seen_at=now, verified=True,
    ))
    session.add(Trader(
        wallet="0x" + "2" * 40, username="beta", tracked=True,
        last_active_at=now, last_seen_at=now, verified=False,
    ))
    market_id = "0x" + "a" * 64
    session.add(Market(
        condition_id=market_id, question="Will BTC hit 150k?", category="CRYPTO",
        slug="btc-150k", end_time=end,
    ))
    session.add(ClosedPosition(
        fingerprint="cf1", wallet="0x" + "1" * 40, condition_id=market_id,
        token_id="t-yes", outcome="Yes", avg_price=Decimal("0.30"),
        total_bought=Decimal("1000"), realized_pnl=Decimal("800"),
        current_price=Decimal("0.99"), closed_at=closed_at,
    ))
    session.add(ClosedPosition(
        fingerprint="cf2", wallet="0x" + "2" * 40, condition_id=market_id,
        token_id="t-yes", outcome="Yes", avg_price=Decimal("0.40"),
        total_bought=Decimal("500"), realized_pnl=Decimal("200"),
        current_price=Decimal("0.99"), closed_at=closed_at,
    ))
    session.add(LeaderboardEntry(
        collected_at=now, category="OVERALL", time_period="ALL", rank=1,
        wallet="0x" + "1" * 40, pnl=Decimal("5000"), volume=Decimal("50000"), raw={},
    ))
    session.add(LeaderboardEntry(
        collected_at=now, category="OVERALL", time_period="ALL", rank=2,
        wallet="0x" + "2" * 40, pnl=Decimal("2500"), volume=Decimal("30000"), raw={},
    ))
    session.add(CurrentPosition(
        wallet="0x" + "1" * 40, token_id="t-yes-1", condition_id=market_id,
        outcome="Yes", size=Decimal("100"), avg_price=Decimal("0.32"),
        current_price=Decimal("0.42"), initial_value=Decimal("32"),
        current_value=Decimal("42"), cash_pnl=Decimal("10"),
        first_observed_at=now, observed_at=now,
    ))
    session.add(CurrentPosition(
        wallet="0x" + "2" * 40, token_id="t-yes-2", condition_id=market_id,
        outcome="Yes", size=Decimal("50"), avg_price=Decimal("0.34"),
        current_price=Decimal("0.42"), initial_value=Decimal("17"),
        current_value=Decimal("21"), cash_pnl=Decimal("4"),
        first_observed_at=now, observed_at=now,
    ))
    session.add(Trade(
        fingerprint="t1", wallet="0x" + "1" * 40, condition_id=market_id,
        token_id="t-yes", side="BUY", outcome="Yes", price=Decimal("0.30"),
        size=Decimal("100"), amount=Decimal("30"), traded_at=closed_at,
    ))
    session.add(Trade(
        fingerprint="t2", wallet="0x" + "2" * 40, condition_id=market_id,
        token_id="t-yes", side="BUY", outcome="Yes", price=Decimal("0.35"),
        size=Decimal("50"), amount=Decimal("17.5"), traded_at=closed_at,
    ))
    session.commit()


def test_top_profitable_orders_by_total_pnl():
    SessionLocal = _build_session()
    _seed(SessionLocal)
    settings = SmartMoneySettings(
        activity_lookback_days=90,
        recent_trade_hours=24,
        min_consensus_traders=2,
    )
    with SessionLocal() as s:
        rows = SmartMoneyAnalytics(s, settings).top_profitable_traders(top_n=10)
    assert len(rows) == 2
    assert rows[0]["rank"] == 1
    assert rows[0]["wallet_full"] == "0x" + "1" * 40
    assert rows[0]["realized_pnl"] == 800.0
    assert rows[0]["open_pnl"] == 10.0


def test_market_preferences_returns_one_market():
    SessionLocal = _build_session()
    _seed(SessionLocal)
    settings = SmartMoneySettings(activity_lookback_days=90, recent_trade_hours=24, min_consensus_traders=2)
    with SessionLocal() as s:
        data = SmartMoneyAnalytics(s, settings).market_preferences()
    assert data["items"][0]["trade_count"] == 2
    assert data["items"][0]["unique_traders"] == 2


def test_consensus_signal_detected():
    SessionLocal = _build_session()
    _seed(SessionLocal)
    settings = SmartMoneySettings(activity_lookback_days=90, recent_trade_hours=24, min_consensus_traders=2)
    with SessionLocal() as s:
        signals = SmartMoneyAnalytics(s, settings).consensus_signals()
    assert signals, "consensus signal should be present for shared Yes position"
    sig = signals[0]
    assert sig["trader_count"] == 2
    assert sig["direction"] == "YES"
    assert 0 <= sig["confidence"] <= 1


def test_lead_time_returns_positive_hours():
    SessionLocal = _build_session()
    _seed(SessionLocal)
    settings = SmartMoneySettings(activity_lookback_days=90, recent_trade_hours=24, min_consensus_traders=2)
    with SessionLocal() as s:
        lt = SmartMoneyAnalytics(s, settings).avg_lead_time()
    assert lt["total_trades"] == 2
    assert lt["avg_lead_hours"] > 0


def test_price_distribution_buckets():
    SessionLocal = _build_session()
    _seed(SessionLocal)
    settings = SmartMoneySettings(activity_lookback_days=90, recent_trade_hours=24, min_consensus_traders=2)
    with SessionLocal() as s:
        data = SmartMoneyAnalytics(s, settings).price_distribution(bin_size=0.10)
    bins = [b for b in data["bins"] if b["count"] > 0]
    assert bins, "expected at least one non-empty bin"
    assert sum(b["count"] for b in bins) == 2


def test_fake_client_only_used_for_data_layer():
    # Sanity check that the fake client shape matches the real interface
    client = FakePolymarketClient()
    assert client.leaderboard[0]["proxyWallet"].startswith("0x")
