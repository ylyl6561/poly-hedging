"""Tests for the new ``WindowScore`` table + window-based follow list.

These verify the user-facing requirement:

* Trader Scores panel and follow list now rank by the *short window*
  composite — ``smart_window_score`` — which is computed from
  trades / closed positions / current positions inside the last
  ``window_days`` (default 30) only.

* Wallets that have *no recent activity* (no Trade inside the window)
  or *no current positions* are filtered out of the follow list.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


# ---------------------------------------------------------------------------
# Pure-Python tests of the follow list filter (no DB)
# ---------------------------------------------------------------------------


def test_refresh_follow_list_filters_stale_wallet():
    """A wallet with high score but NO current positions AND
    days_since_active > follow_max_idle_days MUST be filtered out.

    This is the user-reported issue: "推荐名单里都是没活跃交易、没持仓
    的账号".
    """
    from smart_money.config import SmartMoneySettings
    from smart_money.followlist import refresh_follow_list
    from smart_money.models import WindowScore

    settings = SmartMoneySettings(
        follow_min_window_score=40.0,
        follow_min_days_active=5,
        follow_min_open_positions=1,
        follow_min_window_pnl=100.0,
        follow_max_idle_days=3,
        follow_min_trade_count=3,
        follow_list_max=10,
        follow_top_n_for_signals=5,
    )

    stale_wallet = "0xSTALE"
    fresh_wallet = "0xFRESH"

    stale_row = WindowScore(
        wallet=stale_wallet,
        username="stale",
        pseudonym=None,
        verified=False,
        window_days=30,
        smart_window_score=85.0,  # high score
        win_rate=0.95,
        roi_pct=300.0,
        realized_pnl=50000.0,
        total_pnl=60000.0,
        unrealized_pnl=10000.0,
        trade_count=200,
        closed_count=180,
        days_active=25,
        open_position_count=0,  # NO positions
        days_since_active=10.0,  # last trade 10d ago
        top_category="crypto",
    )
    fresh_row = WindowScore(
        wallet=fresh_wallet,
        username="fresh",
        pseudonym=None,
        verified=False,
        window_days=30,
        smart_window_score=50.0,
        win_rate=0.80,
        roi_pct=80.0,
        realized_pnl=2000.0,
        total_pnl=3500.0,
        unrealized_pnl=1500.0,
        trade_count=20,
        closed_count=14,
        days_active=8,
        open_position_count=5,
        days_since_active=1.0,
        top_category="sports",
    )

    captured = {"deleted": False, "rows": []}

    class _FakeSess:
        def execute(self, stmt):
            # First call: the SELECT candidates query.
            sql = str(stmt)
            if "smart_money_window_scores" in sql and "FROM" in sql:
                return _Res([fresh_row, stale_row])
            return _Res([])

        def bulk_save_objects(self, objs):
            captured["rows"].extend(objs)
            return len(objs)

    class _Res:
        def __init__(self, items):
            self._items = items

        def all(self):
            return self._items

        def scalars(self):
            return self

        def scalar(self):
            return 1

    sess = _FakeSess()
    top = refresh_follow_list(sess, settings)
    # Both rows come back from the SELECT — but stale is dropped
    # by open_pos<1 + idle>3d filters.
    assert top == [fresh_wallet], f"expected only fresh, got {top}"
    # Make sure the persisted entry only contains the fresh wallet.
    saved_wallets = {r.wallet for r in captured["rows"]}
    assert saved_wallets == {fresh_wallet}, saved_wallets


def test_refresh_follow_list_filters_low_score():
    """A wallet with sub-threshold score must NOT be on the list."""
    from smart_money.config import SmartMoneySettings
    from smart_money.followlist import refresh_follow_list
    from smart_money.models import WindowScore

    settings = SmartMoneySettings(
        follow_min_window_score=40.0,
        follow_min_days_active=5,
        follow_min_open_positions=1,
        follow_min_window_pnl=100.0,
        follow_max_idle_days=3,
        follow_min_trade_count=3,
        follow_list_max=10,
        follow_top_n_for_signals=5,
    )

    class _FakeSess:
        def execute(self, stmt):
            return _Res()

    class _Res:
        def all(self):
            return [
                WindowScore(
                    wallet="0xLOW",
                    username="low",
                    pseudonym=None,
                    verified=False,
                    window_days=30,
                    smart_window_score=20.0,  # below threshold
                    win_rate=0.5,
                    roi_pct=5.0,
                    realized_pnl=50.0,
                    total_pnl=80.0,
                    unrealized_pnl=30.0,
                    trade_count=10,
                    closed_count=8,
                    days_active=2,
                    open_position_count=3,
                    days_since_active=0.5,
                    top_category="x",
                )
            ]

        def scalars(self):
            return self

        def scalar(self):
            return 1

    top = refresh_follow_list(_FakeSess(), settings)
    assert top == []


def test_refresh_follow_list_filters_low_window_pnl():
    """Wallets with positive ROI but tiny absolute PnL must NOT pass
    (the user specifically asked for "盈利最高" — absolute $$)."""
    from smart_money.config import SmartMoneySettings
    from smart_money.followlist import refresh_follow_list
    from smart_money.models import WindowScore

    settings = SmartMoneySettings(
        follow_min_window_score=10.0,  # low — so score alone doesn't drop
        follow_min_days_active=5,
        follow_min_open_positions=1,
        follow_min_window_pnl=100.0,
        follow_max_idle_days=3,
        follow_min_trade_count=3,
        follow_list_max=10,
        follow_top_n_for_signals=5,
    )

    class _FakeSess:
        def execute(self, stmt):
            return _Res()

    class _Res:
        def all(self):
            return [
                WindowScore(
                    wallet="0xPENNY",
                    username="penny",
                    pseudonym=None,
                    verified=False,
                    window_days=30,
                    smart_window_score=15.0,
                    win_rate=0.95,
                    roi_pct=2000.0,  # big ROI
                    realized_pnl=2.0,  # ... but tiny absolute pnl
                    total_pnl=4.0,
                    unrealized_pnl=2.0,
                    trade_count=5,
                    closed_count=4,
                    days_active=5,
                    open_position_count=2,
                    days_since_active=0.5,
                    top_category="x",
                )
            ]

        def scalars(self):
            return self

        def scalar(self):
            return 1

    top = refresh_follow_list(_FakeSess(), settings)
    assert top == [], f"penny wallet should be dropped, got {top}"


def test_window_score_default_thresholds_match_user_intent():
    """The default settings should encode the user's intent verbatim:
    * 30-day window
    * at least 5 active days
    * at least 1 current position
    * at least $100 absolute PnL in window
    * active in the last 3 days
    """
    from smart_money.config import SmartMoneySettings

    s = SmartMoneySettings()
    assert s.follow_min_window_score == 40.0
    assert s.follow_min_days_active == 5
    assert s.follow_min_open_positions == 1
    assert s.follow_min_window_pnl == 100.0
    assert s.follow_max_idle_days == 3
    assert s.follow_min_trade_count == 3
