"""
Tests for multi-window (5m / 15m / 1h) configuration support.

设计原则：因为 ``run_event`` 一次只处理一个事件，策略参数全部沿用统一的
``dual_wallet_*`` 名称。  不同窗口（5m / 15m / 1h）的差异化调参通过
:data:`core.constants.WINDOW_BASELINE_CONFIG` 在 ``resolve_window_overrides``
里自动应用，缺失字段统一回退到 ``dual_wallet_*`` 的全局默认。

These tests verify:

1. ``parse_windows`` correctly parses a comma-separated window list
2. ``resolve_window_overrides`` applies ``WINDOW_BASELINE_CONFIG`` per window
   with proper fallback to ``dual_wallet_*``
3. ``TaskManagerConfig.from_config_dict`` honors the ``window`` parameter via
   the unified ``dual_wallet_*`` keys
4. ``EventTask`` carries the ``window`` label through to summary / repr
5. The default baseline is sane for every supported window
6. No per-window (`5m_` / `15m_` / `1h_`) keys leak into CONFIG_SCHEMA
"""

import os
from unittest.mock import patch

import pytest

from core.config import (
    ACTIVE_WINDOWS,
    CONFIG_SCHEMA,
    parse_windows,
    resolve_window_overrides,
)
from core.constants import (
    SUPPORTED_WINDOWS,
    WINDOW_BASELINE_CONFIG,
    WINDOW_SECONDS,
    get_window_baseline,
    list_supported_windows,
)
from strategy.event_task import EventTask
from strategy.task_manager import TaskManagerConfig


# ===== parse_windows =====

class TestParseWindows:
    def test_parses_csv_string(self):
        assert parse_windows("5m,15m,1h") == ["5m", "15m", "1h"]

    def test_handles_whitespace(self):
        assert parse_windows("5m, 15m , 1h") == ["5m", "15m", "1h"]

    def test_deduplicates(self):
        assert parse_windows("5m,15m,5m,1h,15m") == ["5m", "15m", "1h"]

    def test_drops_unknown_windows(self):
        assert parse_windows("5m,unknown,15m") == ["5m", "15m"]

    def test_falls_back_to_5m_for_empty(self):
        assert parse_windows("") == ["5m"]
        assert parse_windows(None) == ["5m"]
        assert parse_windows([]) == ["5m"]

    def test_accepts_list(self):
        assert parse_windows(["5m", "1h"]) == ["5m", "1h"]
        assert parse_windows(("15m",)) == ["15m"]

    def test_lowercases(self):
        assert parse_windows("5M,15M") == ["5m", "15m"]


# ===== get_window_baseline =====

class TestGetWindowBaseline:
    def test_known_window_field(self):
        assert get_window_baseline("5m", "entry_timeout_sec") == 120
        assert get_window_baseline("15m", "entry_timeout_sec") == 240
        assert get_window_baseline("1h", "entry_timeout_sec") == 900

    def test_unknown_field(self):
        assert get_window_baseline("5m", "does_not_exist") is None

    def test_unknown_window(self):
        assert get_window_baseline("7m", "entry_timeout_sec") is None
        assert get_window_baseline("", "entry_timeout_sec") is None

    def test_list_supported_windows(self):
        assert list_supported_windows() == SUPPORTED_WINDOWS
        assert "5m" in list_supported_windows()
        assert "15m" in list_supported_windows()
        assert "1h" in list_supported_windows()


# ===== resolve_window_overrides =====

class TestResolveWindowOverrides:
    def test_returns_baseline_when_config_empty(self):
        for window in ("5m", "15m", "1h"):
            cfg = resolve_window_overrides({}, window)
            assert cfg["window"] == window
            # Entry timeout must match the per-window baseline.
            assert cfg["entry_timeout_sec"] == WINDOW_BASELINE_CONFIG[window]["entry_timeout_sec"]

    def test_user_dual_wallet_override_takes_precedence(self):
        # User explicitly sets dual_wallet_entry_timeout_sec; it must beat the
        # window-specific baseline (regardless of which window we ask for).
        cfg = {"dual_wallet_entry_timeout_sec": 999}
        for window in ("5m", "15m", "1h"):
            resolved = resolve_window_overrides(cfg, window)
            assert resolved["entry_timeout_sec"] == 999, window

    def test_dual_wallet_override_multiple_fields(self):
        cfg = {
            "dual_wallet_entry_timeout_sec": 555,
            "dual_wallet_min_seconds_before_start": 90,
            "dual_wallet_fixed_sell_price": 0.42,
        }
        resolved = resolve_window_overrides(cfg, "15m")
        assert resolved["entry_timeout_sec"] == 555
        assert resolved["min_seconds_before_start"] == 90
        assert abs(resolved["fixed_sell_price"] - 0.42) < 1e-9

    def test_resolve_all_windows_independently(self):
        # With no overrides, each window picks its own baseline.
        for window in ("5m", "15m", "1h"):
            resolved = resolve_window_overrides({}, window)
            assert resolved["window"] == window
            assert resolved["entry_timeout_sec"] == WINDOW_BASELINE_CONFIG[window]["entry_timeout_sec"]

    def test_unknown_window_falls_back_to_schema_default(self):
        # ``7m`` is not a supported window.  The function should still return
        # something sensible (here it falls back to the CONFIG_SCHEMA default).
        resolved = resolve_window_overrides({}, "7m")
        # Just make sure no crash and the resolution succeeded.
        assert "entry_timeout_sec" in resolved

    def test_result_uses_canonical_field_names(self):
        # The returned dict should use canonical (no-prefix) names, so that
        # downstream code can read `entry_timeout_sec` directly.
        resolved = resolve_window_overrides({}, "5m")
        for canonical in (
            "entry_timeout_sec",
            "force_close_window_sec",
            "min_seconds_before_start",
            "entry_shares",
            "entry_up_price",
            "entry_down_price",
            "fixed_sell_price",
            "max_consecutive_losses",
        ):
            assert canonical in resolved, f"Missing canonical field {canonical}"


# ===== ACTIVE_WINDOWS global =====

class TestActiveWindowsGlobal:
    def test_default_has_at_least_one_window(self):
        # ACTIVE_WINDOWS should default to all supported windows unless
        # explicitly overridden via env / config.json.
        assert isinstance(ACTIVE_WINDOWS, list)
        assert len(ACTIVE_WINDOWS) >= 1
        for window in ACTIVE_WINDOWS:
            assert window in WINDOW_SECONDS


# ===== CONFIG_SCHEMA: no per-window leak =====

class TestConfigSchemaIsUnified:
    @pytest.mark.parametrize("window", ["5m", "15m", "1h"])
    def test_no_per_window_timeout_key(self, window):
        # Per-window keys (e.g. ``5m_entry_timeout_sec``) must NOT exist.
        # All tuning goes through the unified ``dual_wallet_*`` keys plus
        # the per-window baseline in ``WINDOW_BASELINE_CONFIG``.
        assert f"{window}_entry_timeout_sec" not in CONFIG_SCHEMA, (
            f"per-window key {window}_entry_timeout_sec should not exist"
        )

    @pytest.mark.parametrize("window", ["5m", "15m", "1h"])
    def test_no_per_window_force_close_key(self, window):
        assert f"{window}_force_close_window_sec" not in CONFIG_SCHEMA

    @pytest.mark.parametrize("window", ["5m", "15m", "1h"])
    def test_no_per_window_poll_interval_key(self, window):
        assert f"{window}_poll_interval_sec" not in CONFIG_SCHEMA

    def test_global_dual_wallet_keys_still_present(self):
        # Backwards compatibility: original dual_wallet_* keys must remain.
        for k in (
            "dual_wallet_entry_timeout_sec",
            "dual_wallet_force_close_window_sec",
            "dual_wallet_poll_interval_sec",
            "dual_wallet_min_seconds_before_start",
            "dual_wallet_entry_shares",
            "dual_wallet_entry_up_price",
            "dual_wallet_entry_down_price",
            "dual_wallet_fixed_sell_price",
            "dual_wallet_max_consecutive_losses",
        ):
            assert k in CONFIG_SCHEMA, f"Missing unified key {k}"

    def test_windows_key_in_schema(self):
        assert "windows" in CONFIG_SCHEMA
        assert "active_window" in CONFIG_SCHEMA


# ===== TaskManagerConfig: per-window loading via dual_wallet_* keys =====

class TestTaskManagerConfigWindowLoading:
    def test_window_param_recorded(self):
        cfg = TaskManagerConfig.from_config_dict({}, window="15m")
        assert cfg.window == "15m"

    def test_dual_wallet_override_used(self):
        cfg = TaskManagerConfig.from_config_dict(
            {"dual_wallet_entry_timeout_sec": 555},
            window="15m",
        )
        assert cfg.entry_timeout_sec == 555

    def test_dual_wallet_override_with_poll_interval(self):
        cfg = TaskManagerConfig.from_config_dict(
            {"dual_wallet_poll_interval_sec": 5.5},
            window="1h",
        )
        assert cfg.poll_interval_sec == 5.5

    def test_baseline_used_when_no_override(self):
        cfg = TaskManagerConfig.from_config_dict({}, window="1h")
        # 1h baseline is 900s
        assert cfg.entry_timeout_sec == 900
        # 1h baseline for poll is 1.0
        assert cfg.poll_interval_sec == 1.0

    def test_global_dual_wallet_fallback_when_no_window_param(self):
        # When window=None we expect the legacy behaviour: pull from dual_wallet_* keys.
        cfg = TaskManagerConfig.from_config_dict(
            {"dual_wallet_entry_timeout_sec": 200}
        )
        assert cfg.entry_timeout_sec == 200

    def test_5m_window_uses_5m_baseline(self):
        cfg = TaskManagerConfig.from_config_dict({}, window="5m")
        assert cfg.entry_timeout_sec == 120  # 5m baseline
        assert cfg.poll_interval_sec == 0.1
        assert cfg.min_seconds_before_start == 30

    def test_15m_window_uses_15m_baseline(self):
        cfg = TaskManagerConfig.from_config_dict({}, window="15m")
        assert cfg.entry_timeout_sec == 240  # 15m baseline
        assert cfg.poll_interval_sec == 0.5
        assert cfg.min_seconds_before_start == 60

    def test_1h_window_uses_1h_baseline(self):
        cfg = TaskManagerConfig.from_config_dict({}, window="1h")
        assert cfg.entry_timeout_sec == 900  # 1h baseline
        assert cfg.poll_interval_sec == 1.0
        assert cfg.min_seconds_before_start == 120

    def test_user_override_beats_baseline_for_every_window(self):
        cfg_dummy = {"dual_wallet_entry_timeout_sec": 123}
        for window in ("5m", "15m", "1h"):
            cfg = TaskManagerConfig.from_config_dict(cfg_dummy, window=window)
            assert cfg.entry_timeout_sec == 123, window

    def test_legacy_call_without_window_still_works(self):
        # Backwards compat: caller did not pass window, just config dict.
        cfg = TaskManagerConfig.from_config_dict({})
        # Should fall back to legacy defaults defined inside from_config_dict
        assert cfg.entry_timeout_sec == 92
        assert cfg.window is None


# ===== EventTask window attribute =====

class TestEventTaskWindow:
    def test_default_window_is_none(self):
        from datetime import datetime, timezone, timedelta
        task = EventTask(
            event_name="X",
            event_id="x1",
            condition_id="cx",
            clob_token_ids=["a", "b"],
            end_time=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        assert task.window is None

    def test_window_set_via_constructor(self):
        from datetime import datetime, timezone, timedelta
        task = EventTask(
            event_name="X",
            event_id="x1",
            condition_id="cx",
            clob_token_ids=["a", "b"],
            end_time=datetime.now(timezone.utc) + timedelta(minutes=10),
            window="15m",
        )
        assert task.window == "15m"

    def test_summary_includes_window(self):
        from datetime import datetime, timezone, timedelta
        task = EventTask(
            event_name="X",
            event_id="x1",
            condition_id="cx",
            clob_token_ids=["a", "b"],
            end_time=datetime.now(timezone.utc) + timedelta(minutes=10),
            window="1h",
        )
        s = task.get_summary()
        assert s["window"] == "1h"

    def test_repr_includes_window(self):
        from datetime import datetime, timezone, timedelta
        task = EventTask(
            event_name="X",
            event_id="x1",
            condition_id="cx",
            clob_token_ids=["a", "b"],
            end_time=datetime.now(timezone.utc) + timedelta(minutes=10),
            window="15m",
        )
        assert "15m" in repr(task)


# ===== Strategy.run_event uses unified parameters =====

class TestStrategyUsesUnifiedParams:
    """Confirm that ``DualWalletEventStrategy`` does not need any per-window
    config keys: the same ``dual_wallet_*`` keys drive every window via the
    baseline auto-resolution.
    """

    def test_dual_wallet_min_seconds_before_start_is_used_for_every_window(self):
        # 15m baseline is 60s, but user sets it to 5s via the unified key.
        from core.config import resolve_window_overrides
        cfg = {"dual_wallet_min_seconds_before_start": 5}
        for window in ("5m", "15m", "1h"):
            r = resolve_window_overrides(cfg, window)
            assert r["min_seconds_before_start"] == 5, window

    def test_no_5m_min_seconds_before_start_needed(self):
        # The user should never have to set ``5m_min_seconds_before_start``;
        # they only ever touch ``dual_wallet_min_seconds_before_start``.
        cfg = {"dual_wallet_min_seconds_before_start": 7}
        r = resolve_window_overrides(cfg, "5m")
        assert r["min_seconds_before_start"] == 7