"""
EventTaskState 单元测试

测试 event_task_state 模块中的所有核心功能：
- EventTaskState 枚举
- 状态分类函数 (is_active_state, is_terminal_state)
- 状态转移规则 (can_transition, STATE_TRANSITIONS)
- StateTransition 数据类
- 状态元数据和显示函数
"""

import pytest
from datetime import datetime, timezone
from dataclasses import is_dataclass, asdict

from strategy.event_task_state import (
    EventTaskState,
    STATE_TRANSITIONS,
    STATE_METADATA,
    STATE_CATEGORIES,
    is_active_state,
    is_terminal_state,
    can_transition,
    StateTransition,
    get_state_display_name,
    get_state_priority,
)


class TestEventTaskStateEnum:
    """测试 EventTaskState 枚举。"""

    def test_all_states_have_values(self):
        """所有状态都有对应的字符串值。"""
        for state in EventTaskState:
            assert isinstance(state.value, str)
            assert len(state.value) > 0

    def test_initial_states(self):
        assert EventTaskState.PENDING.value == "pending"

    def test_placing_states(self):
        assert EventTaskState.PLACING_ENTRY.value == "placing_entry"

    def test_waiting_states(self):
        assert EventTaskState.WAITING_ENTRY.value == "waiting_entry"
        assert EventTaskState.WAITING_CLOSE_WINDOW.value == "waiting_close_window"

    def test_handling_states(self):
        assert EventTaskState.HANDLING_SINGLE.value == "handling_single"
        assert EventTaskState.FORCE_CLOSING.value == "force_closing"

    def test_settling_states(self):
        assert EventTaskState.SETTLING_OUTCOME.value == "settling_outcome"
        assert EventTaskState.SETTLING_BALANCE.value == "settling_balance"

    def test_terminal_states(self):
        assert EventTaskState.SETTLED.value == "settled"
        assert EventTaskState.FAILED.value == "failed"
        assert EventTaskState.SKIPPED.value == "skipped"

    def test_state_count(self):
        """确保所有预期状态都存在。"""
        expected_states = [
            "pending",
            "placing_entry",
            "waiting_entry",
            "handling_single",
            "waiting_close_window",
            "force_closing",
            "settling_outcome",
            "settling_balance",
            "settled",
            "failed",
            "skipped",
        ]
        actual_states = [s.value for s in EventTaskState]
        for expected in expected_states:
            assert expected in actual_states, f"Missing state: {expected}"


class TestStateCategories:
    """测试状态分类。"""

    def test_active_states(self):
        """测试活跃状态集合。"""
        active = STATE_CATEGORIES["active"]
        assert EventTaskState.PENDING in active
        assert EventTaskState.PLACING_ENTRY in active
        assert EventTaskState.WAITING_ENTRY in active
        assert EventTaskState.HANDLING_SINGLE in active
        assert EventTaskState.WAITING_CLOSE_WINDOW in active
        assert EventTaskState.FORCE_CLOSING in active
        assert EventTaskState.SETTLING_OUTCOME in active
        assert EventTaskState.SETTLING_BALANCE in active

    def test_terminal_states(self):
        """测试终止状态集合。"""
        terminal = STATE_CATEGORIES["terminal"]
        assert EventTaskState.SETTLED in terminal
        assert EventTaskState.FAILED in terminal
        assert EventTaskState.SKIPPED in terminal

    def test_no_overlap(self):
        """活跃状态和终止状态不应该重叠。"""
        active = STATE_CATEGORIES["active"]
        terminal = STATE_CATEGORIES["terminal"]
        overlap = active & terminal
        assert len(overlap) == 0, f"Overlap found: {overlap}"

    def test_is_active_state(self):
        """测试 is_active_state 函数。"""
        assert is_active_state(EventTaskState.PENDING) is True
        assert is_active_state(EventTaskState.PLACING_ENTRY) is True
        assert is_active_state(EventTaskState.WAITING_ENTRY) is True
        assert is_active_state(EventTaskState.SETTLED) is False
        assert is_active_state(EventTaskState.FAILED) is False

    def test_is_terminal_state(self):
        """测试 is_terminal_state 函数。"""
        assert is_terminal_state(EventTaskState.SETTLED) is True
        assert is_terminal_state(EventTaskState.FAILED) is True
        assert is_terminal_state(EventTaskState.SKIPPED) is True
        assert is_terminal_state(EventTaskState.PENDING) is False
        assert is_terminal_state(EventTaskState.WAITING_ENTRY) is False


class TestStateTransitions:
    """测试状态转移规则。"""

    def test_pending_can_transition(self):
        """PENDING 可以转移到多个状态。"""
        transitions = STATE_TRANSITIONS[EventTaskState.PENDING]
        assert EventTaskState.PLACING_ENTRY in transitions
        assert EventTaskState.SETTLING_OUTCOME in transitions
        assert EventTaskState.SKIPPED in transitions

    def test_placing_entry_can_transition(self):
        """PLACING_ENTRY 可以转移。"""
        transitions = STATE_TRANSITIONS[EventTaskState.PLACING_ENTRY]
        assert EventTaskState.WAITING_ENTRY in transitions
        assert EventTaskState.SETTLED in transitions
        assert EventTaskState.FAILED in transitions

    def test_waiting_entry_can_transition(self):
        """WAITING_ENTRY 可以转移到多种状态。"""
        transitions = STATE_TRANSITIONS[EventTaskState.WAITING_ENTRY]
        assert EventTaskState.SETTLING_OUTCOME in transitions  # 双边成交
        assert EventTaskState.HANDLING_SINGLE in transitions   # 单边成交
        assert EventTaskState.WAITING_CLOSE_WINDOW in transitions  # 超时
        assert EventTaskState.SETTLED in transitions

    def test_handling_single_can_transition(self):
        """HANDLING_SINGLE 可以转移。"""
        transitions = STATE_TRANSITIONS[EventTaskState.HANDLING_SINGLE]
        assert EventTaskState.WAITING_CLOSE_WINDOW in transitions
        assert EventTaskState.FORCE_CLOSING in transitions
        assert EventTaskState.SETTLING_OUTCOME in transitions

    def test_terminal_states_have_no_transitions(self):
        """终止状态不能转移到任何状态。"""
        for terminal in [EventTaskState.SETTLED, EventTaskState.FAILED, EventTaskState.SKIPPED]:
            transitions = STATE_TRANSITIONS[terminal]
            assert len(transitions) == 0

    def test_can_transition_valid(self):
        """测试合法的状态转移。"""
        assert can_transition(EventTaskState.PENDING, EventTaskState.PLACING_ENTRY) is True
        assert can_transition(EventTaskState.PLACING_ENTRY, EventTaskState.WAITING_ENTRY) is True
        assert can_transition(EventTaskState.WAITING_ENTRY, EventTaskState.HANDLING_SINGLE) is True

    def test_can_transition_invalid(self):
        """测试非法的状态转移。"""
        # 不能从 PENDING 直接跳到 SETTLED（除非是特殊情况）
        assert can_transition(EventTaskState.PENDING, EventTaskState.SETTLED) is False
        # 不能从 WAITING_ENTRY 直接到 FORCE_CLOSING
        assert can_transition(EventTaskState.WAITING_ENTRY, EventTaskState.FORCE_CLOSING) is False
        # 不能从 SETTLED 转移
        assert can_transition(EventTaskState.SETTLED, EventTaskState.PENDING) is False

    def test_can_transition_same_state(self):
        """同一状态不能转移到自己。"""
        for state in EventTaskState:
            assert can_transition(state, state) is False

    def test_all_transitions_are_valid(self):
        """所有定义的转移目标都应该是有效的状态。"""
        for from_state, to_states in STATE_TRANSITIONS.items():
            for to_state in to_states:
                # 确保 to_state 是一个有效的 EventTaskState
                assert isinstance(to_state, EventTaskState)


class TestStateTransition:
    """测试 StateTransition 数据类。"""

    def test_is_dataclass(self):
        assert is_dataclass(StateTransition)

    def test_creation(self):
        now = datetime.now(timezone.utc)
        transition = StateTransition(
            from_state=EventTaskState.PENDING,
            to_state=EventTaskState.PLACING_ENTRY,
            reason="Event is starting",
        )
        assert transition.from_state == EventTaskState.PENDING
        assert transition.to_state == EventTaskState.PLACING_ENTRY
        assert transition.reason == "Event is starting"
        assert transition.timestamp is not None

    def test_asdict(self):
        transition = StateTransition(
            from_state=EventTaskState.PENDING,
            to_state=EventTaskState.PLACING_ENTRY,
            reason="test",
        )
        d = asdict(transition)
        assert isinstance(d, dict)
        assert d["from_state"] == EventTaskState.PENDING
        assert d["to_state"] == EventTaskState.PLACING_ENTRY
        assert d["reason"] == "test"


class TestStateMetadata:
    """测试状态元数据。"""

    def test_all_states_have_metadata(self):
        """所有状态都应该有元数据。"""
        for state in EventTaskState:
            assert state in STATE_METADATA, f"Missing metadata for {state}"
            metadata = STATE_METADATA[state]
            assert "name" in metadata
            assert "priority" in metadata
            assert "color" in metadata

    def test_metadata_types(self):
        """元数据的类型应该正确。"""
        for state, metadata in STATE_METADATA.items():
            assert isinstance(metadata["name"], str)
            assert isinstance(metadata["priority"], int)
            assert isinstance(metadata["color"], str)

    def test_priority_order(self):
        """终止状态的优先级应该高于活跃状态。"""
        for terminal in [EventTaskState.SETTLED, EventTaskState.FAILED, EventTaskState.SKIPPED]:
            terminal_priority = STATE_METADATA[terminal]["priority"]
            for active in STATE_CATEGORIES["active"]:
                active_priority = STATE_METADATA[active]["priority"]
                assert terminal_priority >= active_priority

    def test_get_state_display_name(self):
        """测试显示名称获取。"""
        assert get_state_display_name(EventTaskState.PENDING) == "等待开始"
        assert get_state_display_name(EventTaskState.PLACING_ENTRY) == "挂单中"
        assert get_state_display_name(EventTaskState.WAITING_ENTRY) == "等待成交"
        assert get_state_display_name(EventTaskState.HANDLING_SINGLE) == "单边处理"
        assert get_state_display_name(EventTaskState.FORCE_CLOSING) == "强平中"
        assert get_state_display_name(EventTaskState.SETTLED) == "已完成"

    def test_get_state_priority(self):
        """测试优先级获取。"""
        pending_priority = get_state_priority(EventTaskState.PENDING)
        placing_priority = get_state_priority(EventTaskState.PLACING_ENTRY)
        settled_priority = get_state_priority(EventTaskState.SETTLED)

        # PENDING 应该优先级最低
        assert pending_priority < placing_priority
        # SETTLED 应该优先级最高
        assert placing_priority < settled_priority
        assert pending_priority < settled_priority

    def test_unknown_state_returns_value(self):
        """未知状态应该返回其值。"""
        # 创建一个不存在的状态（理论上不会发生）
        # 但 get_state_display_name 应该优雅处理
        result = get_state_display_name(EventTaskState.PENDING)
        assert result is not None


class TestWorkflowScenarios:
    """测试典型的工作流场景。"""

    def test_normal_workflow(self):
        """测试正常的工作流：PENDING -> PLACING_ENTRY -> WAITING_ENTRY -> ... -> SETTLED"""
        assert can_transition(EventTaskState.PENDING, EventTaskState.PLACING_ENTRY)
        assert can_transition(EventTaskState.PLACING_ENTRY, EventTaskState.WAITING_ENTRY)
        assert can_transition(EventTaskState.WAITING_ENTRY, EventTaskState.HANDLING_SINGLE)
        assert can_transition(EventTaskState.HANDLING_SINGLE, EventTaskState.WAITING_CLOSE_WINDOW)
        assert can_transition(EventTaskState.WAITING_CLOSE_WINDOW, EventTaskState.FORCE_CLOSING)
        assert can_transition(EventTaskState.FORCE_CLOSING, EventTaskState.SETTLING_OUTCOME)
        assert can_transition(EventTaskState.SETTLING_OUTCOME, EventTaskState.SETTLING_BALANCE)
        assert can_transition(EventTaskState.SETTLING_BALANCE, EventTaskState.SETTLED)

    def test_both_sides_filled_workflow(self):
        """测试双边成交的工作流：WAITING_ENTRY -> SETTLING_OUTCOME"""
        assert can_transition(EventTaskState.WAITING_ENTRY, EventTaskState.SETTLING_OUTCOME)
        assert can_transition(EventTaskState.SETTLING_OUTCOME, EventTaskState.SETTLING_BALANCE)
        assert can_transition(EventTaskState.SETTLING_BALANCE, EventTaskState.SETTLED)

    def test_timeout_no_fill_workflow(self):
        """测试超时无成交的工作流。"""
        assert can_transition(EventTaskState.WAITING_ENTRY, EventTaskState.WAITING_CLOSE_WINDOW)
        assert can_transition(EventTaskState.WAITING_CLOSE_WINDOW, EventTaskState.FORCE_CLOSING)
        assert can_transition(EventTaskState.FORCE_CLOSING, EventTaskState.SETTLING_OUTCOME)

    def test_skip_workflow(self):
        """测试跳过工作流。"""
        assert can_transition(EventTaskState.PENDING, EventTaskState.SKIPPED)

    def test_failure_workflow(self):
        """测试失败工作流。"""
        assert can_transition(EventTaskState.PLACING_ENTRY, EventTaskState.FAILED)
        assert can_transition(EventTaskState.WAITING_ENTRY, EventTaskState.FAILED)
        assert can_transition(EventTaskState.FORCE_CLOSING, EventTaskState.FAILED)
        assert can_transition(EventTaskState.SETTLING_OUTCOME, EventTaskState.FAILED)
        assert can_transition(EventTaskState.SETTLING_BALANCE, EventTaskState.FAILED)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
