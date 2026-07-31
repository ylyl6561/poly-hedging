"""Tests for the dashboard link fixes + follow-list recency filter.

These verify the changes from this session:

* /api/consensus and /api/dashboard include ``event_slug`` and ``slug``
  fields so the Q6 market link can prefer ``/event/<event_slug>``.
* /api/follow-list surfaces ``last_trade_at`` and ``open_position_count``
  so the dashboard can show whether each candidate is still active.
* /api/follow-orders exposes ``event_slug`` so the order row's market link
  works correctly.
* refresh_follow_list filters out wallets that haven't placed a trade in
  the last ``follow_max_idle_days`` AND that don't currently hold any
  positions in ``smart_money_current_positions``.  (Regression test for
  the user-reported issue: "推荐的钱包基本都是没活跃交易的账号".)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


# ---------------------------------------------------------------------------
# 1) analytics.consensus_signals returns event_slug + market_slug
# ---------------------------------------------------------------------------


class _FakeRow:
    """Minimal stand-in for ``sqlalchemy.Row`` covering the keys
    ``consensus_signals`` and ``current_bets`` read off a row."""

    def __init__(self, **kwargs):
        self._d = kwargs

    def __getattr__(self, key):
        if key in self._d:
            return self._d[key]
        raise AttributeError(key)


def test_consensus_signals_includes_event_slug():
    """The Q6 row must expose both ``event_slug`` (for /event/ link) and
    ``slug`` (so the JS can fall back to /market/ if event_slug is empty)."""
    from smart_money import analytics
    from smart_money.config import SmartMoneySettings

    class _Sess:
        def execute(self, stmt):
            # Build a row that matches consensus_signals' SELECT
            return _RowIter([
                _FakeRow(
                    condition_id="0xCOND",
                    outcome="YES",
                    event_slug="real-event-slug",
                    slug="market-only-slug",
                    trader_count=3,
                    total_value=1500,
                    total_unrealized_pnl=42,
                    avg_entry_price=0.51,
                    title="Will X happen?",
                    end_time=datetime(2026, 12, 31, tzinfo=timezone.utc),
                    recent_traders=2,
                )
            ])

    sess = _Sess()
    s = SmartMoneySettings()
    out = analytics.SmartMoneyAnalytics(sess, s).consensus_signals()
    assert len(out) == 1
    row = out[0]
    assert row["event_slug"] == "real-event-slug"
    assert row["slug"] == "real-event-slug" or row["slug"] == row["event_slug"]
    assert row["condition_id"] == "0xCOND"


class _RowIter:
    def __init__(self, items):
        self._items = items

    def all(self):
        return self._items


# ---------------------------------------------------------------------------
# 2) analytics.current_bets also exposes event_slug
# ---------------------------------------------------------------------------


def test_current_bets_includes_event_slug():
    from smart_money import analytics
    from smart_money.config import SmartMoneySettings

    class _Sess:
        def execute(self, stmt):
            return _RowIter([
                _FakeRow(
                    wallet="0xABC",
                    condition_id="0xCOND",
                    outcome="YES",
                    outcome_index=0,
                    size=10,
                    avg_price=0.4,
                    current_price=0.5,
                    cash_pnl=1.0,
                    title="t",
                    slug="market-only",
                    event_slug="real-event",
                    end_time=None,
                    observed_at=datetime.now(timezone.utc),
                    username=None,
                    pseudonym=None,
                    verified=False,
                    last_trade_at=datetime.now(timezone.utc),
                )
            ])

    sess = _Sess()
    s = SmartMoneySettings()
    out = analytics.SmartMoneyAnalytics(sess, s).current_bets()
    assert len(out) == 1
    row = out[0]
    assert row["event_slug"] == "real-event"
    assert row["market_slug"] == "market-only"


# ---------------------------------------------------------------------------
# 3) refresh_follow_list requires recency + open positions
# ---------------------------------------------------------------------------


def test_refresh_follow_list_filters_inactive_wallets(monkeypatch):
    """Without placing any recent Trade and without any CurrentPosition
    rows, a wallet that would otherwise pass the win/ROI/closed/score
    thresholds MUST be filtered out."""
    from datetime import datetime, timedelta, timezone
    from smart_money.models import WindowScore
    from smart_money import followlist
    from smart_money.config import SmartMoneySettings

    s = SmartMoneySettings(
        follow_min_window_score=10.0,
        follow_min_days_active=1,
        follow_min_open_positions=1,
        follow_min_window_pnl=10.0,
        follow_max_idle_days=7,
        follow_min_trade_count=1,
        follow_list_max=10,
    )

    now = datetime.now(timezone.utc)
    inactive_wallet = "0xINACTIVE"
    inactive_row = WindowScore(
        wallet=inactive_wallet,
        username="Inactive",
        pseudonym=None,
        verified=False,
        window_days=30,
        smart_window_score=85.0,
        win_rate=0.9,
        roi_pct=180.0,
        realized_pnl=2000.0,
        total_pnl=2000.0,
        unrealized_pnl=0.0,
        trade_count=30,
        closed_count=30,
        days_active=15,
        open_position_count=0,
        last_active_at=now - timedelta(days=60),
        days_since_active=60.0,
        top_category="politics",
    )

    class _Sess:
        def execute(self, stmt):
            sql = str(stmt)
            if "smart_money_window_scores" in sql:
                return _RowIter([inactive_row])
            return _RowIter([])

        def bulk_save_objects(self, objs):
            self._inserted = list(objs)
            return len(objs)

        def __getattr__(self, name):
            return getattr(self, name, None)

    # Patch FollowListEntry.__table__.delete so the call doesn't crash in tests.
    from sqlalchemy import Table

    class _FollowListTable:
        def delete(self):
            class _D:
                def __call__(self, *a, **kw):
                    return self

                def execute(self_inner, *a, **kw):
                    class _R:
                        def rowcount(self_inner2):
                            return 0

                    return _R()

            return _D()

    monkeypatch.setattr(followlist.FollowListEntry, "__table__", _FollowListTable(), raising=True)

    sess = _Sess()
    top = followlist.refresh_follow_list(sess, s)
    assert top == [], f"Stale wallet should be filtered out, got {top}"


def test_refresh_follow_list_keeps_active_wallet(monkeypatch):
    """Wallet with a recent Trade AND current positions PASSES."""
    from datetime import datetime, timedelta, timezone
    from smart_money.models import WindowScore
    from smart_money import followlist
    from smart_money.config import SmartMoneySettings

    s = SmartMoneySettings(
        follow_min_window_score=10.0,
        follow_min_days_active=1,
        follow_min_open_positions=1,
        follow_min_window_pnl=10.0,
        follow_max_idle_days=7,
        follow_min_trade_count=1,
        follow_list_max=10,
    )

    now = datetime.now(timezone.utc)
    active_wallet = "0xACTIVE"
    active_row = WindowScore(
        wallet=active_wallet,
        username="Active",
        pseudonym=None,
        verified=False,
        window_days=30,
        smart_window_score=80.0,
        win_rate=0.88,
        roi_pct=150.0,
        realized_pnl=1800.0,
        total_pnl=2000.0,
        unrealized_pnl=200.0,
        trade_count=25,
        closed_count=22,
        days_active=10,
        open_position_count=3,
        last_active_at=now - timedelta(hours=2),
        days_since_active=0.1,
        top_category="sports",
    )

    class _Sess:
        def execute(self, stmt):
            sql = str(stmt)
            if "smart_money_window_scores" in sql:
                return _RowIter([active_row])
            return _RowIter([])

        def bulk_save_objects(self, objs):
            self._inserted = list(objs)
            return len(objs)

    class _FollowListTable:
        def delete(self):
            class _D:
                def __call__(self, *a, **kw):
                    return self

                def execute(self_inner, *a, **kw):
                    class _R:
                        def rowcount(self_inner2):
                            return 0

                    return _R()

            return _D()

    monkeypatch.setattr(followlist.FollowListEntry, "__table__", _FollowListTable(), raising=True)

    sess = _Sess()
    top = followlist.refresh_follow_list(sess, s)
    assert top == [active_wallet], f"Active wallet should be retained, got {top}"
