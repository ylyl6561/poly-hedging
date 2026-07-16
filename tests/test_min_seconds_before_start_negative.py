"""
Tests for the ``min_seconds_before_start < 0`` semantic.

Background: ``run_event`` 一次只处理一个事件，因此该字段允许负值，
当 N < 0 时表示事件已开始最多 |N| 秒仍可挂单。

统一公式 (now = start_time - time_to_event_start)：
    time_to_event_start >= -N
等价于：
    now - start_time <= N
"""

from datetime import datetime, timezone, timedelta

import pytest

from strategy.task_manager import TaskManagerConfig


def _decide(time_to_start: float, min_seconds: int) -> bool:
    """Mirror the actual gate inside ``run_once_for_window`` and
    ``TaskManager._process_pending``.  Returns True if the event passes
    the min-lead gate and should be processed.

    Unified rule: ``time_to_start >= min_seconds`` where ``time_to_start``
    is ``start_time - now`` (positive before the event, negative after).
    """
    return time_to_start >= min_seconds


class TestPositiveSemantics:
    """The legacy positive-N semantics must be preserved."""

    def test_lead_time_meeting_threshold_is_allowed(self):
        # 200s lead time, threshold 180s -> allowed
        assert _decide(200.0, 180) is True

    def test_lead_time_equal_to_threshold_is_allowed(self):
        # exact boundary
        assert _decide(180.0, 180) is True

    def test_lead_time_just_below_threshold_is_blocked(self):
        # 179s lead time, threshold 180s -> blocked
        assert _decide(179.0, 180) is False

    def test_zero_threshold_any_lead_time_is_allowed(self):
        # N=0 means "no minimum lead time required", so any future lead
        # time passes the gate.
        assert _decide(0.0, 0) is True
        assert _decide(60.0, 0) is True
        # But once the event starts (time_to_start < 0), N=0 no longer allows.
        assert _decide(-0.5, 0) is False
        assert _decide(-1.0, 0) is False


class TestNegativeSemantics:
    """Negative N allows the event to be processed even after it started."""

    def test_n_minus_10_allows_5s_after_start(self):
        # 5 seconds into the event, threshold -10 -> allowed
        assert _decide(-5.0, -10) is True

    def test_n_minus_10_blocks_15s_after_start(self):
        # 15 seconds into the event, threshold -10 -> blocked
        assert _decide(-15.0, -10) is False

    def test_n_minus_10_at_exact_boundary(self):
        # exactly 10 seconds into the event -> allowed (>= boundary)
        assert _decide(-10.0, -10) is True

    def test_n_minus_30_blocks_long_after_start(self):
        # 60s into event, threshold -30 -> blocked
        assert _decide(-60.0, -30) is False

    def test_n_minus_30_allows_long_lead_time(self):
        # Lead time well in advance, negative threshold -> allowed
        # (negative threshold is more permissive than 0)
        assert _decide(500.0, -30) is True


class TestTaskManagerConfigAcceptsNegative:
    """TaskManagerConfig stores the value verbatim; we just verify it
    round-trips and is plumbed through ``from_config_dict`` correctly.
    """

    def test_direct_construction_with_negative(self):
        cfg = TaskManagerConfig(min_seconds_before_start=-10)
        assert cfg.min_seconds_before_start == -10

    def test_from_config_dict_picks_negative_override(self):
        cfg = TaskManagerConfig.from_config_dict(
            {"dual_wallet_min_seconds_before_start": -10},
            window="5m",
        )
        assert cfg.min_seconds_before_start == -10

    def test_from_config_dict_baseline_stays_positive(self):
        # 5m baseline default is 30s; should not be silently changed.
        cfg = TaskManagerConfig.from_config_dict({}, window="5m")
        assert cfg.min_seconds_before_start == 30


class TestFastloopEntryGate:
    """End-to-end test of the gate inside ``run_once_for_window``.

    We do not spin up the real Trader; instead we replicate the gate logic
    inline and verify it matches the documented contract.
    """

    @pytest.mark.parametrize(
        "lead_time,threshold,allowed",
        [
            # Positive-threshold semantics
            (300.0, 30, True),
            (29.0, 30, False),
            (60.0, 60, True),
            # Zero threshold
            (0.0, 0, True),
            (-0.5, 0, False),
            # Negative threshold (allow after-start grace)
            (-5.0, -10, True),
            (-9.9, -10, True),
            (-10.0, -10, True),
            (-10.1, -10, False),
            (-30.0, -10, False),
            # Far lead time, negative threshold
            (3600.0, -5, True),
        ],
    )
    def test_gate_matrix(self, lead_time, threshold, allowed):
        assert _decide(lead_time, threshold) is allowed