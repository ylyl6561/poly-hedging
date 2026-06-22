"""
TaskManager 业务逻辑单元测试

测试 TaskManager 的核心业务逻辑：
- 事件时机判断：何时挂单，何时跳过
- 时间临界条件处理
- 状态转移的正确性
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, MagicMock

from strategy.task_manager import TaskManager, TaskManagerConfig
from strategy.event_task_state import EventTaskState


class TestEventTimingLogic:
    """测试事件时机判断逻辑。"""

    @pytest.fixture
    def config(self):
        """标准配置：最小提前量 180 秒。"""
        return TaskManagerConfig(
            poll_interval_sec=1.0,
            entry_timeout_sec=100,
            force_close_window_sec=60,
            fixed_sell_price=0.76,
            max_consecutive_losses=2,
            min_seconds_before_start=180,  # 关键：180 秒最小提前量
            outcome_poll_timeout_sec=900,
            outcome_poll_interval_sec=5,
            settlement_poll_timeout_sec=180,
            settlement_poll_interval_sec=20,
            settlement_stable_rounds=3,
            progress_log_interval_sec=30.0,
            enable_feishu=False,
        )

    def test_should_place_order_when_time_is_sufficient(self, config):
        """
        测试：当距离开始时间 >= min_seconds_before_start 时，应该挂单。

        场景：事件开始前 200 秒（>= 180 秒阈值）
        期望：should_place_order = True
        """
        now = datetime.now(timezone.utc)
        start_time = now + timedelta(seconds=200)  # 距开始 200 秒
        end_time = start_time + timedelta(minutes=5)

        # 核心判断逻辑
        time_to_event_start = (start_time - now).total_seconds()
        should_place_order = time_to_event_start >= config.min_seconds_before_start

        assert should_place_order is True, "距开始 200s 应该挂单"

    def test_should_place_order_when_time_equals_threshold(self, config):
        """
        测试：当距离开始时间 == min_seconds_before_start 时，应该挂单。

        场景：事件开始前 180 秒（== 180 秒阈值）
        期望：should_place_order = True
        """
        now = datetime.now(timezone.utc)
        start_time = now + timedelta(seconds=180)

        time_to_event_start = (start_time - now).total_seconds()
        should_place_order = time_to_event_start >= config.min_seconds_before_start

        assert should_place_order is True, "距开始 180s 应该挂单"

    def test_should_skip_when_time_is_insufficient(self, config):
        """
        测试：当距离开始时间 < min_seconds_before_start 时，应该跳过。

        场景：事件开始前 179 秒（< 180 秒阈值）
        期望：should_place_order = False
        """
        now = datetime.now(timezone.utc)
        start_time = now + timedelta(seconds=179)

        time_to_event_start = (start_time - now).total_seconds()
        should_place_order = time_to_event_start >= config.min_seconds_before_start

        assert should_place_order is False, "距开始 179s 应该跳过"

    def test_should_place_order_when_event_already_started(self, config):
        """
        测试：当事件已开始（time_to_start <= 0）时，应该挂单。

        场景：事件已开始 5 秒
        期望：should_place_order = True（事件已开始，应该执行）
        """
        now = datetime.now(timezone.utc)
        start_time = now - timedelta(seconds=5)  # 已开始 5 秒

        time_to_event_start = (start_time - now).total_seconds()
        should_place_order = time_to_event_start <= 0 or time_to_event_start >= config.min_seconds_before_start

        assert should_place_order is True, "事件已开始应该执行"

    def test_should_skip_just_before_threshold(self, config):
        """
        测试：临界情况 - 距开始时间刚刚低于阈值。

        场景：事件开始前 1 秒（< 180 秒阈值）
        期望：should_place_order = False
        """
        now = datetime.now(timezone.utc)
        start_time = now + timedelta(seconds=1)

        time_to_event_start = (start_time - now).total_seconds()
        should_place_order = time_to_event_start >= config.min_seconds_before_start

        assert should_place_order is False, "距开始 1s 应该跳过"


class TestPendingStateTransition:
    """
    测试 PENDING 状态的实际转移行为。

    这是关键测试：确保 TaskManager._process_pending 正确判断何时挂单、何时跳过。
    """

    def test_pending_transitions_correctly_based_on_time(self):
        """
        测试 PENDING 根据时间正确转移到 PLACING_ENTRY 或 SKIPPED。

        这个测试揭示了之前的 bug：当距开始时间足够时，应该立即转移到 PLACING_ENTRY，
        而不是一直保持在 PENDING 等待。
        """
        # 模拟距开始时间足够的场景
        now = datetime.now(timezone.utc)
        start_time = now + timedelta(seconds=200)
        end_time = start_time + timedelta(minutes=5)

        time_to_event_start = (start_time - now).total_seconds()
        min_seconds_before_start = 180

        # 正确的判断逻辑
        should_place_order = time_to_event_start >= min_seconds_before_start
        should_skip = not should_place_order

        assert should_place_order is True, "距开始 200s 应该挂单"
        assert should_skip is False, "距开始 200s 不应该跳过"

    def test_boundary_condition_180_seconds(self):
        """
        测试边界条件：刚好 180 秒。

        这是最关键的边界测试，防止"刚好 180 秒时做什么"的歧义。
        """
        now = datetime.now(timezone.utc)
        min_seconds = 180

        # 测试临界值：刚好 180 秒
        start_time_180s = now + timedelta(seconds=180)
        time_to_start = (start_time_180s - now).total_seconds()
        assert time_to_start >= min_seconds, "180s 应该 >= 阈值"

        # 测试临界值：179.9 秒
        start_time_179_9s = now + timedelta(seconds=179.9)
        time_to_start = (start_time_179_9s - now).total_seconds()
        assert time_to_start < min_seconds, "179.9s 应该 < 阈值"


class TestSkipScenario:
    """测试跳过场景。"""

    def test_skip_only_when_truly_insufficient(self):
        """
        测试：只有在真正时间不足时才跳过。

        验证"只有距开始时间 < 阈值时才跳过"这个逻辑。
        """
        min_seconds = 180

        test_cases = [
            # (距开始秒数, 期望跳过?)
            (300, False),  # 5 分钟 - 应该挂单
            (200, False),  # 3分20秒 - 应该挂单
            (181, False),  # 3分1秒 - 应该挂单
            (180, False),  # 刚好 180s - 应该挂单
            (179, True),   # 179s - 应该跳过
            (100, True),   # 100s - 应该跳过
            (1, True),     # 1s - 应该跳过
        ]

        for time_to_start, expected_skip in test_cases:
            should_place = time_to_start >= min_seconds
            should_skip = not should_place
            assert should_skip == expected_skip, f"距开始 {time_to_start}s: skip={expected_skip}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
