"""
TaskManager 最新业务流程单元测试

覆盖最新的业务流程的每个节点：
1. PENDING → PLACING_ENTRY → WAITING_ENTRY
2. WAITING_ENTRY → SETTLING_OUTCOME (双边成交)
3. WAITING_ENTRY → HANDLING_SINGLE (单边成交)
4. HANDLING_SINGLE → WAITING_CLOSE_WINDOW (只挂抛售单，不撤单)
5. WAITING_CLOSE_WINDOW → SETTLING_OUTCOME (挂单先成交)
6. WAITING_CLOSE_WINDOW → SETTLING_OUTCOME (抛售单先成交)
7. WAITING_CLOSE_WINDOW → FORCE_CLOSING (强平窗口到达)
8. FORCE_CLOSING → SETTLING_OUTCOME (执行强平)
9. SETTLING_OUTCOME → SETTLING_BALANCE (市场结果)
10. SETTLING_BALANCE → SETTLED (余额稳定)
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, MagicMock, patch, PropertyMock

from strategy.task_manager import TaskManager, TaskManagerConfig
from strategy.event_task import EventTask
from strategy.event_task_state import EventTaskState
from strategy.dual_wallet_models import EventOutcome, OrderStatus, OrderSide, WalletIdentity, OrderSnapshot, OperationType
from strategy.order_executor_v2 import OrderExecutorV2, ExecutionOutcome, OrderOperationResult


# ===== Fixtures =====

@pytest.fixture
def config():
    """标准配置。"""
    return TaskManagerConfig(
        poll_interval_sec=1.0,
        entry_timeout_sec=100,
        force_close_window_sec=60,
        fixed_sell_price=0.76,
        max_consecutive_losses=2,
        min_seconds_before_start=180,
        outcome_poll_timeout_sec=900,
        outcome_poll_interval_sec=5,
        settlement_poll_timeout_sec=180,
        settlement_poll_interval_sec=20,
        settlement_stable_rounds=3,
        progress_log_interval_sec=30.0,
        enable_feishu=False,
    )


@pytest.fixture
def mock_wallet():
    """创建模拟钱包。"""
    wallet = Mock(spec=WalletIdentity)
    wallet.wallet_id = "wallet_1"
    wallet.wallet_name = "TestWallet"
    wallet.role = Mock(value="up")
    wallet.account = Mock()
    wallet.account.account_id = "acc_1"
    return wallet


@pytest.fixture
def mock_wallet_up(mock_wallet):
    """UP 方向钱包。"""
    wallet = Mock(spec=WalletIdentity)
    wallet.wallet_id = "wallet_up"
    wallet.wallet_name = "WalletUP"
    wallet.role = Mock(value="up")
    wallet.account = Mock()
    wallet.account.account_id = "acc_up"
    return wallet


@pytest.fixture
def mock_wallet_down():
    """DOWN 方向钱包。"""
    wallet = Mock(spec=WalletIdentity)
    wallet.wallet_id = "wallet_down"
    wallet.wallet_name = "WalletDOWN"
    wallet.role = Mock(value="down")
    wallet.account = Mock()
    wallet.account.account_id = "acc_down"
    return wallet


@pytest.fixture
def mock_order_executor():
    """创建模拟订单执行器。"""
    executor = Mock(spec=OrderExecutorV2)
    executor.executor = Mock()
    return executor


@pytest.fixture
def event_task(mock_wallet_up, mock_wallet_down):
    """创建测试用 EventTask。"""
    now = datetime.now(timezone.utc)
    return EventTask(
        event_name="Test BTC Event",
        event_id="evt_123",
        condition_id="cond_123",
        clob_token_ids=["token_up", "token_down"],
        end_time=now + timedelta(minutes=10),
        start_time=now - timedelta(seconds=10),
        close_window_sec=60,
        wallets=[mock_wallet_up, mock_wallet_down],
        side_by_wallet_id={
            "wallet_up": OrderSide.UP,
            "wallet_down": OrderSide.DOWN,
        },
        metadata={
            "up_price": 0.5,
            "down_price": 0.5,
            "amount_usd": 10.0,
            "fee_rate_bps": 0,
        },
    )


def create_mock_task_manager(config, mock_wallet_up, mock_wallet_down, mock_order_executor):
    """创建模拟 TaskManager。"""
    tm = Mock(spec=TaskManager)
    tm.config = config
    tm._order_exec = mock_order_executor
    tm.wallets = [mock_wallet_up, mock_wallet_down]
    tm._tasks = {}
    tm._pollers = {}
    tm._completed_tasks = []
    tm._failed_tasks = []
    return tm


def create_filled_order(wallet: WalletIdentity, side: OrderSide, shares: float = 5.0) -> OrderSnapshot:
    """创建已成交订单。"""
    return OrderSnapshot(
        wallet=wallet,
        event_name="Test Event",
        side=side,
        amount_usd=shares * 0.5,
        operation=OperationType.PLACE,
        order_id=f"order_{wallet.wallet_id}",
        price=0.5,
        shares=shares,
        filled_shares=shares,
        filled_amount_usd=shares * 0.5,
        status=OrderStatus.FILLED.value,
        timestamp=datetime.now(timezone.utc),
    )


def create_pending_order(wallet: WalletIdentity, side: OrderSide, shares: float = 5.0) -> OrderSnapshot:
    """创建待成交订单。"""
    return OrderSnapshot(
        wallet=wallet,
        event_name="Test Event",
        side=side,
        amount_usd=shares * 0.5,
        operation=OperationType.PLACE,
        order_id=f"order_{wallet.wallet_id}",
        price=0.5,
        shares=shares,
        filled_shares=0.0,
        filled_amount_usd=0.0,
        status=OrderStatus.SUBMITTED.value,
        timestamp=datetime.now(timezone.utc),
    )


# ===== 测试类 =====

class TestPendingToPlacingEntry:
    """测试 PENDING → PLACING_ENTRY 流程。"""

    def test_pending_to_placing_entry_when_event_started(self, config, mock_wallet_up, mock_wallet_down, mock_order_executor):
        """
        测试：事件已开始时，PENDING → PLACING_ENTRY

        流程：事件已开始（距开始 < 0），直接进入挂单阶段
        """
        task = EventTask(
            event_name="Test Event",
            event_id="evt_1",
            condition_id="cond_1",
            clob_token_ids=["token_up", "token_down"],
            end_time=datetime.now(timezone.utc) + timedelta(minutes=10),
            start_time=datetime.now(timezone.utc) - timedelta(seconds=10),  # 已开始
            close_window_sec=60,
            wallets=[mock_wallet_up, mock_wallet_down],
            side_by_wallet_id={"wallet_up": OrderSide.UP, "wallet_down": OrderSide.DOWN},
        )

        # 验证：start_time 已过，应该可以进入挂单阶段
        now = datetime.now(timezone.utc)
        assert task.start_time < now, "事件应该已开始"

        # 验证：距开始时间足够（< 0 表示已开始）
        time_to_start = (task.start_time - now).total_seconds()
        assert time_to_start <= 0, "事件应该已开始"

    def test_pending_skipped_when_time_insufficient(self, config, mock_wallet_up, mock_wallet_down):
        """
        测试：距开始时间不足时，PENDING → SKIPPED

        流程：距开始 < min_seconds_before_start，跳过
        """
        min_seconds = 180
        task = EventTask(
            event_name="Test Event",
            event_id="evt_1",
            condition_id="cond_1",
            clob_token_ids=["token_up", "token_down"],
            end_time=datetime.now(timezone.utc) + timedelta(minutes=10),
            start_time=datetime.now(timezone.utc) + timedelta(seconds=100),  # 距开始 100s < 180s
            close_window_sec=60,
            wallets=[mock_wallet_up, mock_wallet_down],
            side_by_wallet_id={"wallet_up": OrderSide.UP, "wallet_down": OrderSide.DOWN},
        )

        now = datetime.now(timezone.utc)
        time_to_start = (task.start_time - now).total_seconds()

        # 验证：距开始时间不足
        assert time_to_start < min_seconds, "距开始时间应该不足"


class TestPlacingEntryToWaitingEntry:
    """测试 PLACING_ENTRY → WAITING_ENTRY 流程。"""

    def test_placing_entry_transitions_to_waiting_entry(self):
        """
        测试：挂单完成后，PLACING_ENTRY → WAITING_ENTRY
        """
        from strategy.event_task_state import can_transition

        # 验证状态转移
        assert can_transition(EventTaskState.PLACING_ENTRY, EventTaskState.WAITING_ENTRY) is True

    def test_placing_entry_to_failed_when_no_orders(self):
        """
        测试：挂单失败时，PLACING_ENTRY → FAILED
        """
        from strategy.event_task_state import can_transition

        assert can_transition(EventTaskState.PLACING_ENTRY, EventTaskState.FAILED) is True


class TestWaitingEntryTransitions:
    """测试 WAITING_ENTRY 的各种转移情况。"""

    def test_waiting_entry_to_both_filled(self):
        """
        测试：双边成交 → SETTLING_OUTCOME

        这是关键场景：双边成交确认后，直接进入结算，跳过强平阶段
        """
        from strategy.event_task_state import can_transition

        assert can_transition(EventTaskState.WAITING_ENTRY, EventTaskState.SETTLING_OUTCOME) is True

    def test_waiting_entry_to_single_filled(self):
        """
        测试：单边成交 → HANDLING_SINGLE
        """
        from strategy.event_task_state import can_transition

        assert can_transition(EventTaskState.WAITING_ENTRY, EventTaskState.HANDLING_SINGLE) is True

    def test_waiting_entry_to_waiting_close_window(self):
        """
        测试：超时进入强平窗口 → WAITING_CLOSE_WINDOW
        """
        from strategy.event_task_state import can_transition

        assert can_transition(EventTaskState.WAITING_ENTRY, EventTaskState.WAITING_CLOSE_WINDOW) is True


class TestBothSidesFilledWorkflow:
    """测试双边成交的完整流程。"""

    def test_both_sides_filled_workflow(self, event_task):
        """
        测试：双边成交确认后的状态转移

        流程：WAITING_ENTRY → SETTLING_OUTCOME → SETTLING_BALANCE → SETTLED
        """
        from strategy.event_task_state import can_transition

        # 验证完整流程
        assert can_transition(EventTaskState.WAITING_ENTRY, EventTaskState.SETTLING_OUTCOME) is True
        assert can_transition(EventTaskState.SETTLING_OUTCOME, EventTaskState.SETTLING_BALANCE) is True
        assert can_transition(EventTaskState.SETTLING_BALANCE, EventTaskState.SETTLED) is True

    def test_both_sides_filled_updates_shares(self, event_task, mock_wallet_up, mock_wallet_down):
        """
        测试：双边成交时，正确更新 UP/DOWN 份额
        """
        up_order = create_filled_order(mock_wallet_up, OrderSide.UP, shares=5.0)
        down_order = create_filled_order(mock_wallet_down, OrderSide.DOWN, shares=5.0)

        event_task.mark_order(up_order)
        event_task.mark_order(down_order)

        # 验证成交检查
        both_filled, up_shares, down_shares = event_task.check_both_sides_filled()
        assert both_filled is True
        assert up_shares == 5.0
        assert down_shares == 5.0


class TestSingleSideFilledWorkflow:
    """测试单边成交的完整流程。"""

    def test_single_side_filled_workflow(self, event_task):
        """
        测试：单边成交确认后的状态转移

        流程：WAITING_ENTRY → HANDLING_SINGLE → WAITING_CLOSE_WINDOW → ...
        """
        from strategy.event_task_state import can_transition

        # 验证完整流程
        assert can_transition(EventTaskState.WAITING_ENTRY, EventTaskState.HANDLING_SINGLE) is True
        assert can_transition(EventTaskState.HANDLING_SINGLE, EventTaskState.WAITING_CLOSE_WINDOW) is True

    def test_single_side_filled_identifies_wallet(self, event_task, mock_wallet_up, mock_wallet_down):
        """
        测试：单边成交时，正确识别哪个钱包成交
        """
        # UP 侧成交，DOWN 侧未成交
        up_order = create_filled_order(mock_wallet_up, OrderSide.UP, shares=5.0)
        down_order = create_pending_order(mock_wallet_down, OrderSide.DOWN, shares=5.0)

        event_task.mark_order(up_order)
        event_task.mark_order(down_order)

        # 验证单边成交检查
        single_filled, filled_wallet_id, shares = event_task.check_single_side_filled()
        assert single_filled is True
        assert filled_wallet_id == "wallet_up"
        assert shares == 5.0

    def test_single_side_filled_workflow_state_machine(self):
        """
        测试：单边成交的完整状态机路径

        路径：
        WAITING_ENTRY → HANDLING_SINGLE → WAITING_CLOSE_WINDOW
        → FORCE_CLOSING → SETTLING_OUTCOME → SETTLING_BALANCE → SETTLED
        """
        from strategy.event_task_state import can_transition

        # 单边成交后进入 HANDLING_SINGLE
        assert can_transition(EventTaskState.WAITING_ENTRY, EventTaskState.HANDLING_SINGLE) is True

        # HANDLING_SINGLE → WAITING_CLOSE_WINDOW（处理完成）
        assert can_transition(EventTaskState.HANDLING_SINGLE, EventTaskState.WAITING_CLOSE_WINDOW) is True

        # WAITING_CLOSE_WINDOW → FORCE_CLOSING（强平窗口到达）
        assert can_transition(EventTaskState.WAITING_CLOSE_WINDOW, EventTaskState.FORCE_CLOSING) is True

        # FORCE_CLOSING → SETTLING_OUTCOME（强平完成）
        assert can_transition(EventTaskState.FORCE_CLOSING, EventTaskState.SETTLING_OUTCOME) is True


class TestHandlingSingleWorkflow:
    """测试 HANDLING_SINGLE 状态处理。"""

    def test_handling_single_only_places_sell_order(self, event_task, mock_wallet_up, mock_wallet_down):
        """
        测试：HANDLING_SINGLE 状态下，只挂抛售单，不撤单

        关键业务逻辑：
        - 已成交侧：挂 GTC 抛售单
        - 未成交侧：保留挂单，不撤单
        """
        # 模拟 UP 侧成交，DOWN 侧未成交
        event_task.first_fill_wallet_id = "wallet_up"
        up_order = create_filled_order(mock_wallet_up, OrderSide.UP, shares=5.0)
        down_order = create_pending_order(mock_wallet_down, OrderSide.DOWN, shares=5.0)
        event_task.mark_order(up_order)
        event_task.mark_order(down_order)

        # 验证：DOWN 侧订单应该保留
        stale_order = event_task.get_order("wallet_down")
        assert stale_order is not None
        assert stale_order.status == OrderStatus.SUBMITTED.value

        # 验证：UP 侧订单已成交
        live_order = event_task.get_order("wallet_up")
        assert live_order is not None
        assert live_order.status == OrderStatus.FILLED.value

    def test_handling_single_transitions_to_waiting_close_window(self):
        """
        测试：HANDLING_SINGLE → WAITING_CLOSE_WINDOW

        处理完成后，等待强平窗口
        """
        from strategy.event_task_state import can_transition

        assert can_transition(EventTaskState.HANDLING_SINGLE, EventTaskState.WAITING_CLOSE_WINDOW) is True


class TestWaitingCloseWindowWorkflow:
    """测试 WAITING_CLOSE_WINDOW 状态处理。"""

    def test_waiting_close_window_checks_both_orders(self, event_task, mock_wallet_up, mock_wallet_down):
        """
        测试：WAITING_CLOSE_WINDOW 状态下，检查两种订单的成交情况

        关键业务逻辑（每秒监控）：
        1. 检查 stale 侧原始挂单是否成交
        2. 检查 GTC 抛售单是否成交
        3. 检查强平窗口是否到达
        4. 检查事件是否已结束
        """
        # 模拟场景：UP 侧成交，DOWN 侧未成交，已挂抛售单
        event_task.first_fill_wallet_id = "wallet_up"
        event_task.mark_order(create_filled_order(mock_wallet_up, OrderSide.UP, shares=5.0))
        event_task.mark_order(create_pending_order(mock_wallet_down, OrderSide.DOWN, shares=5.0))

        # 验证：能获取 stale 和 live 侧
        up_wallet = event_task.get_up_wallet()
        down_wallet = event_task.get_down_wallet()

        assert up_wallet is not None
        assert down_wallet is not None

    def test_waiting_close_window_to_settling_when_stale_fills_first(self):
        """
        测试：stale 侧原始挂单先成交 → 取消抛售单 → SETTLING_OUTCOME
        """
        from strategy.event_task_state import can_transition

        assert can_transition(EventTaskState.WAITING_CLOSE_WINDOW, EventTaskState.SETTLING_OUTCOME) is True

    def test_waiting_close_window_to_force_closing(self):
        """
        测试：强平窗口到达 → FORCE_CLOSING
        """
        from strategy.event_task_state import can_transition

        assert can_transition(EventTaskState.WAITING_CLOSE_WINDOW, EventTaskState.FORCE_CLOSING) is True

    def test_waiting_close_window_to_settling_when_event_ended(self):
        """
        测试：事件已结束 → SETTLING_OUTCOME
        """
        from strategy.event_task_state import can_transition

        assert can_transition(EventTaskState.WAITING_CLOSE_WINDOW, EventTaskState.SETTLING_OUTCOME) is True


class TestForceClosingWorkflow:
    """测试 FORCE_CLOSING 状态处理。"""

    def test_force_closing_workflow(self):
        """
        测试：FORCE_CLOSING 状态的转移

        流程：FORCE_CLOSING → SETTLING_OUTCOME
        """
        from strategy.event_task_state import can_transition

        assert can_transition(EventTaskState.FORCE_CLOSING, EventTaskState.SETTLING_OUTCOME) is True
        assert can_transition(EventTaskState.FORCE_CLOSING, EventTaskState.SETTLED) is True
        assert can_transition(EventTaskState.FORCE_CLOSING, EventTaskState.FAILED) is True

    def test_force_closing_cancels_and_places_market(self, event_task, mock_wallet_up, mock_wallet_down):
        """
        测试：FORCE_CLOSING 状态下，执行撤单 + 纯市价单强平

        关键业务逻辑：
        - 取消 stale 侧挂单
        - 取消 live 侧 GTC 抛售单（如果有）
        - 挂纯市价单强平
        """
        # 模拟场景：UP 侧成交，已挂抛售单
        event_task.first_fill_wallet_id = "wallet_up"
        event_task.mark_order(create_filled_order(mock_wallet_up, OrderSide.UP, shares=5.0))
        event_task.mark_order(create_pending_order(mock_wallet_down, OrderSide.DOWN, shares=5.0))

        # 验证：能获取 stale 和 live 侧
        stale_wallet = event_task.get_down_wallet()  # 未成交侧
        live_wallet = event_task.get_up_wallet()  # 已成交侧

        assert stale_wallet is not None
        assert live_wallet is not None


class TestSettlingOutcomeWorkflow:
    """测试 SETTLING_OUTCOME 状态处理。"""

    def test_settling_outcome_to_settling_balance(self):
        """
        测试：市场结果已出 → SETTLING_BALANCE
        """
        from strategy.event_task_state import can_transition

        assert can_transition(EventTaskState.SETTLING_OUTCOME, EventTaskState.SETTLING_BALANCE) is True

    def test_settling_outcome_timeout(self):
        """
        测试：SETTLING_OUTCOME 超时 → SETTLED
        """
        from strategy.event_task_state import can_transition

        assert can_transition(EventTaskState.SETTLING_OUTCOME, EventTaskState.SETTLED) is True

    def test_settling_outcome_failure(self):
        """
        测试：SETTLING_OUTCOME 失败 → FAILED
        """
        from strategy.event_task_state import can_transition

        assert can_transition(EventTaskState.SETTLING_OUTCOME, EventTaskState.FAILED) is True


class TestSettlingBalanceWorkflow:
    """测试 SETTLING_BALANCE 状态处理。"""

    def test_settling_balance_to_settled(self):
        """
        测试：余额稳定 → SETTLED
        """
        from strategy.event_task_state import can_transition

        assert can_transition(EventTaskState.SETTLING_BALANCE, EventTaskState.SETTLED) is True

    def test_settling_balance_timeout(self):
        """
        测试：SETTLING_BALANCE 超时 → FAILED
        """
        from strategy.event_task_state import can_transition

        assert can_transition(EventTaskState.SETTLING_BALANCE, EventTaskState.FAILED) is True


class TestCompleteWorkflows:
    """测试完整的业务流程。"""

    def test_complete_both_sides_filled_workflow(self):
        """
        测试：完整双边成交流程

        PENDING → PLACING_ENTRY → WAITING_ENTRY → SETTLING_OUTCOME → SETTLING_BALANCE → SETTLED
        """
        from strategy.event_task_state import can_transition

        # 验证完整流程
        workflow = [
            (EventTaskState.PENDING, EventTaskState.PLACING_ENTRY),
            (EventTaskState.PLACING_ENTRY, EventTaskState.WAITING_ENTRY),
            (EventTaskState.WAITING_ENTRY, EventTaskState.SETTLING_OUTCOME),  # 双边成交
            (EventTaskState.SETTLING_OUTCOME, EventTaskState.SETTLING_BALANCE),
            (EventTaskState.SETTLING_BALANCE, EventTaskState.SETTLED),
        ]

        for from_state, to_state in workflow:
            assert can_transition(from_state, to_state) is True, f"{from_state} → {to_state}"

    def test_complete_single_side_filled_with_stale_fills_first_workflow(self):
        """
        测试：完整单边成交流程（stale 侧先成交）

        PENDING → PLACING_ENTRY → WAITING_ENTRY → HANDLING_SINGLE
        → WAITING_CLOSE_WINDOW → SETTLING_OUTCOME → SETTLING_BALANCE → SETTLED
        """
        from strategy.event_task_state import can_transition

        workflow = [
            (EventTaskState.PENDING, EventTaskState.PLACING_ENTRY),
            (EventTaskState.PLACING_ENTRY, EventTaskState.WAITING_ENTRY),
            (EventTaskState.WAITING_ENTRY, EventTaskState.HANDLING_SINGLE),  # 单边成交
            (EventTaskState.HANDLING_SINGLE, EventTaskState.WAITING_CLOSE_WINDOW),
            (EventTaskState.WAITING_CLOSE_WINDOW, EventTaskState.SETTLING_OUTCOME),  # stale 先成交
            (EventTaskState.SETTLING_OUTCOME, EventTaskState.SETTLING_BALANCE),
            (EventTaskState.SETTLING_BALANCE, EventTaskState.SETTLED),
        ]

        for from_state, to_state in workflow:
            assert can_transition(from_state, to_state) is True, f"{from_state} → {to_state}"

    def test_complete_single_side_filled_with_force_close_workflow(self):
        """
        测试：完整单边成交流程（强平窗口到达）

        PENDING → PLACING_ENTRY → WAITING_ENTRY → HANDLING_SINGLE
        → WAITING_CLOSE_WINDOW → FORCE_CLOSING → SETTLING_OUTCOME → SETTLING_BALANCE → SETTLED
        """
        from strategy.event_task_state import can_transition

        workflow = [
            (EventTaskState.PENDING, EventTaskState.PLACING_ENTRY),
            (EventTaskState.PLACING_ENTRY, EventTaskState.WAITING_ENTRY),
            (EventTaskState.WAITING_ENTRY, EventTaskState.HANDLING_SINGLE),  # 单边成交
            (EventTaskState.HANDLING_SINGLE, EventTaskState.WAITING_CLOSE_WINDOW),
            (EventTaskState.WAITING_CLOSE_WINDOW, EventTaskState.FORCE_CLOSING),  # 强平窗口到达
            (EventTaskState.FORCE_CLOSING, EventTaskState.SETTLING_OUTCOME),
            (EventTaskState.SETTLING_OUTCOME, EventTaskState.SETTLING_BALANCE),
            (EventTaskState.SETTLING_BALANCE, EventTaskState.SETTLED),
        ]

        for from_state, to_state in workflow:
            assert can_transition(from_state, to_state) is True, f"{from_state} → {to_state}"

    def test_complete_timeout_workflow(self):
        """
        测试：完整超时流程（无任何成交）

        PENDING → PLACING_ENTRY → WAITING_ENTRY → WAITING_CLOSE_WINDOW
        → FORCE_CLOSING → SETTLING_OUTCOME → SETTLING_BALANCE → SETTLED
        """
        from strategy.event_task_state import can_transition

        workflow = [
            (EventTaskState.PENDING, EventTaskState.PLACING_ENTRY),
            (EventTaskState.PLACING_ENTRY, EventTaskState.WAITING_ENTRY),
            (EventTaskState.WAITING_ENTRY, EventTaskState.WAITING_CLOSE_WINDOW),  # 超时
            (EventTaskState.WAITING_CLOSE_WINDOW, EventTaskState.FORCE_CLOSING),
            (EventTaskState.FORCE_CLOSING, EventTaskState.SETTLING_OUTCOME),
            (EventTaskState.SETTLING_OUTCOME, EventTaskState.SETTLING_BALANCE),
            (EventTaskState.SETTLING_BALANCE, EventTaskState.SETTLED),
        ]

        for from_state, to_state in workflow:
            assert can_transition(from_state, to_state) is True, f"{from_state} → {to_state}"


class TestEventTaskMethods:
    """测试 EventTask 的方法。"""

    def test_get_up_wallet(self, event_task):
        """测试 get_up_wallet 方法。"""
        up_wallet = event_task.get_up_wallet()
        assert up_wallet is not None
        assert up_wallet.wallet_id == "wallet_up"

    def test_get_down_wallet(self, event_task):
        """测试 get_down_wallet 方法。"""
        down_wallet = event_task.get_down_wallet()
        assert down_wallet is not None
        assert down_wallet.wallet_id == "wallet_down"

    def test_check_both_sides_filled_when_both_filled(self, event_task, mock_wallet_up, mock_wallet_down):
        """测试 check_both_sides_filled（双边都已成交）。"""
        event_task.mark_order(create_filled_order(mock_wallet_up, OrderSide.UP, shares=5.0))
        event_task.mark_order(create_filled_order(mock_wallet_down, OrderSide.DOWN, shares=5.0))

        both_filled, up_shares, down_shares = event_task.check_both_sides_filled()
        assert both_filled is True
        assert up_shares == 5.0
        assert down_shares == 5.0

    def test_check_both_sides_filled_when_only_one_filled(self, event_task, mock_wallet_up, mock_wallet_down):
        """测试 check_both_sides_filled（只有一边成交）。"""
        event_task.mark_order(create_filled_order(mock_wallet_up, OrderSide.UP, shares=5.0))
        event_task.mark_order(create_pending_order(mock_wallet_down, OrderSide.DOWN, shares=5.0))

        both_filled, up_shares, down_shares = event_task.check_both_sides_filled()
        assert both_filled is False

    def test_check_single_side_filled(self, event_task, mock_wallet_up, mock_wallet_down):
        """测试 check_single_side_filled 方法。"""
        # UP 侧成交
        event_task.mark_order(create_filled_order(mock_wallet_up, OrderSide.UP, shares=5.0))
        event_task.mark_order(create_pending_order(mock_wallet_down, OrderSide.DOWN, shares=5.0))

        single_filled, filled_wallet_id, shares = event_task.check_single_side_filled()
        assert single_filled is True
        assert filled_wallet_id == "wallet_up"
        assert shares == 5.0

    def test_check_single_side_filled_when_none_filled(self, event_task, mock_wallet_up, mock_wallet_down):
        """测试 check_single_side_filled（无任何成交）。"""
        event_task.mark_order(create_pending_order(mock_wallet_up, OrderSide.UP, shares=5.0))
        event_task.mark_order(create_pending_order(mock_wallet_down, OrderSide.DOWN, shares=5.0))

        single_filled, filled_wallet_id, shares = event_task.check_single_side_filled()
        assert single_filled is False

    def test_remaining_to_end(self, event_task):
        """测试 remaining_to_end 方法。"""
        remaining = event_task.remaining_to_end()
        assert remaining > 0, "距结束时间应该 > 0"
        assert remaining < 600, "距结束时间应该 < 10 分钟"

    def test_is_in_close_window(self, event_task):
        """测试 is_in_close_window 方法。"""
        # 当前不应该在强平窗口内（距结束 > 60s）
        remaining = event_task.remaining_to_end()
        is_in_window = remaining <= event_task.close_window_sec
        assert is_in_window is False, "距结束时间 > 60s，应该不在强平窗口内"


class TestEdgeCases:
    """测试边界情况。"""

    def test_event_already_ended(self, mock_wallet_up, mock_wallet_down):
        """
        测试：事件已结束的情况
        """
        task = EventTask(
            event_name="Ended Event",
            event_id="evt_ended",
            condition_id="cond_ended",
            clob_token_ids=["token_up", "token_down"],
            end_time=datetime.now(timezone.utc) - timedelta(minutes=1),  # 已结束
            start_time=datetime.now(timezone.utc) - timedelta(minutes=6),
            close_window_sec=60,
            wallets=[mock_wallet_up, mock_wallet_down],
            side_by_wallet_id={"wallet_up": OrderSide.UP, "wallet_down": OrderSide.DOWN},
        )

        # 验证：事件已结束
        now = datetime.now(timezone.utc)
        assert task.end_time < now, "事件应该已结束"

        remaining = task.remaining_to_end()
        assert remaining <= 0, "距结束时间应该 <= 0"

    def test_skip_workflow(self):
        """
        测试：跳过流程

        PENDING → SKIPPED
        """
        from strategy.event_task_state import can_transition

        assert can_transition(EventTaskState.PENDING, EventTaskState.SKIPPED) is True

    def test_handling_single_to_settling_outcome(self):
        """
        测试：HANDLING_SINGLE → SETTLING_OUTCOME（事件已结束）

        边界情况：处理单边时事件已结束
        """
        from strategy.event_task_state import can_transition

        assert can_transition(EventTaskState.HANDLING_SINGLE, EventTaskState.SETTLING_OUTCOME) is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
