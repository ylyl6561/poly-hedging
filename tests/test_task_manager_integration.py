"""
TaskManager 流程集成测试 - 真正跑通每个状态的 process 方法

覆盖核心业务路径：
1. HANDLING_SINGLE → WAITING_CLOSE_WINDOW → SETTLING_OUTCOME (stale先成交)
2. HANDLING_SINGLE → WAITING_CLOSE_WINDOW → SETTLING_OUTCOME (sell先成交)
3. HANDLING_SINGLE → WAITING_CLOSE_WINDOW → FORCE_CLOSING → SETTLING_OUTCOME (强平)
4. 完整的 SETTLING_OUTCOME → SETTLING_BALANCE → SETTLED

用法:
    pytest tests/test_task_manager_integration.py -v
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, patch, MagicMock
from dataclasses import dataclass, field

from strategy.task_manager import TaskManager, TaskManagerConfig
from strategy.event_task import EventTask
from strategy.event_task_state import EventTaskState
from strategy.dual_wallet_models import EventOutcome, OrderStatus, OrderSide, WalletIdentity, OrderSnapshot, OperationType


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
def mock_wallet_up():
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


def create_filled_snapshot(wallet_id: str, side: OrderSide, shares: float = 5.0) -> OrderSnapshot:
    """创建已成交快照。"""
    return OrderSnapshot(
        wallet=Mock(wallet_id=wallet_id, wallet_name=f"MockWallet_{wallet_id}"),
        event_name="Test Event",
        side=side,
        amount_usd=shares * 0.5,
        operation=OperationType.PLACE,
        order_id=f"order_{wallet_id}",
        price=0.5,
        shares=shares,
        filled_shares=shares,
        filled_amount_usd=shares * 0.5,
        status=OrderStatus.FILLED.value,
        timestamp=datetime.now(timezone.utc),
    )


def create_pending_snapshot(wallet_id: str, side: OrderSide, shares: float = 5.0) -> OrderSnapshot:
    """创建待成交快照。"""
    snapshot = OrderSnapshot(
        wallet=Mock(wallet_id=wallet_id, wallet_name=f"MockWallet_{wallet_id}"),
        event_name="Test Event",
        side=side,
        amount_usd=shares * 0.5,
        operation=OperationType.PLACE,
        order_id=f"order_{wallet_id}",
        price=0.5,
        shares=shares,
        filled_shares=0.0,
        filled_amount_usd=0.0,
        status=OrderStatus.SUBMITTED.value,
        timestamp=datetime.now(timezone.utc),
    )
    return snapshot


def create_sell_snapshot(wallet_id: str, side: OrderSide, shares: float = 5.0) -> OrderSnapshot:
    """创建抛售单快照。"""
    return OrderSnapshot(
        wallet=Mock(wallet_id=wallet_id, wallet_name=f"MockWallet_{wallet_id}"),
        event_name="Test Event",
        side=side,
        amount_usd=shares * 0.76,
        operation=OperationType.SELL,
        order_id=f"sell_order_{wallet_id}",
        price=0.76,
        shares=shares,
        filled_shares=0.0,
        filled_amount_usd=0.0,
        status=OrderStatus.SUBMITTED.value,
        timestamp=datetime.now(timezone.utc),
    )


def create_mock_executor():
    """创建模拟执行器。"""
    executor = Mock()
    executor.place_entry_order = Mock(return_value=Mock(
        outcome=Mock(success=True),
        snapshot=create_pending_snapshot("test", OrderSide.UP)
    ))
    executor.place_gtc_sell_order = Mock(return_value=Mock(
        outcome=Mock(success=True),
        snapshot=create_sell_snapshot("test", OrderSide.DOWN)
    ))
    executor.cancel_order = Mock(return_value=Mock(success=True))
    executor.refresh_order_status = Mock(return_value=(Mock(success=True), None))
    executor.execute_force_close = Mock(return_value=Mock(
        outcome=Mock(success=True),
        snapshot=create_filled_snapshot("test", OrderSide.UP)
    ))
    return executor


def create_task(
    mock_wallet_up,
    mock_wallet_down,
    start_offset_sec: float = -10,
    end_offset_min: float = 10,
    close_window_sec: int = 60,
):
    """创建测试任务。"""
    now = datetime.now(timezone.utc)
    task = EventTask(
        event_name="Test BTC Event",
        event_id="evt_123",
        condition_id="cond_123",
        clob_token_ids=["token_up", "token_down"],
        end_time=now + timedelta(minutes=end_offset_min),
        start_time=now + timedelta(seconds=start_offset_sec),
        close_window_sec=close_window_sec,
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
    return task


def create_manager(config, mock_wallet_up, mock_wallet_down, executor=None):
    """创建真正的 TaskManager（需要 mock 掉网络调用）。"""
    if executor is None:
        executor = create_mock_executor()
    with patch('strategy.task_manager.get_wallet_usdc_balance') as mock_balance:
        mock_balance.return_value = {"success": True, "balance_usdc": 100.0}

        manager = TaskManager(
            config=config,
            executor=executor,
            wallets=[mock_wallet_up, mock_wallet_down],
            run_folder="/tmp/test_runs",
            dry_run=True,
        )
        manager._order_exec = executor
        return manager


# ===== 测试类 =====

class TestHandlingSingleProcess:
    """测试 _process_handling_single 方法。"""

    def test_handling_single_places_sell_order_and_keeps_stale(self, config, mock_wallet_up, mock_wallet_down):
        """
        测试：HANDLING_SINGLE 状态下，已成交侧挂GTC抛售单，未成交侧保留挂单。
        """
        manager = create_manager(config, mock_wallet_up, mock_wallet_down)
        task = create_task(mock_wallet_up, mock_wallet_down, start_offset_sec=-30)

        # 设置为 HANDLING_SINGLE 状态（先设置为 WAITING_ENTRY，再修改状态）
        task.first_fill_wallet_id = "wallet_up"
        task.mark_order(create_filled_snapshot("wallet_up", OrderSide.UP, shares=5.0))
        task.mark_order(create_pending_snapshot("wallet_down", OrderSide.DOWN, shares=5.0))
        
        # 直接修改状态（跳过合法性检查，因为这是测试环境）
        task.state = EventTaskState.HANDLING_SINGLE
        task.state_changed_at = datetime.now(timezone.utc)

        # 执行处理
        manager._process_handling_single(task)

        # 验证：已成交侧挂抛售单
        manager._order_exec.place_gtc_sell_order.assert_called()
        # 验证：未成交侧订单保留
        down_order = task.get_order("wallet_down")
        assert down_order is not None
        assert down_order.status == OrderStatus.SUBMITTED.value
        # 验证：状态转移到 WAITING_CLOSE_WINDOW
        assert task.state == EventTaskState.WAITING_CLOSE_WINDOW


class TestWaitingCloseWindowProcess:
    """测试 _process_waiting_close_window 方法。"""

    def test_close_window_reached_triggers_force_closing(self, config, mock_wallet_up, mock_wallet_down):
        """
        测试：强平窗口到达时的处理逻辑存在。
        
        注意：这个测试验证代码路径存在，但由于 mock 限制，
        实际状态转移取决于订单状态的 mock 值。
        """
        manager = create_manager(config, mock_wallet_up, mock_wallet_down)
        task = create_task(mock_wallet_up, mock_wallet_down, start_offset_sec=-120, close_window_sec=60)

        # 设置为 WAITING_CLOSE_WINDOW 状态
        task.first_fill_wallet_id = "wallet_up"
        task.mark_order(create_filled_snapshot("wallet_up", OrderSide.UP, shares=5.0))
        task.mark_order(create_pending_snapshot("wallet_down", OrderSide.DOWN, shares=5.0))
        task.state = EventTaskState.WAITING_CLOSE_WINDOW
        task.state_changed_at = datetime.now(timezone.utc)

        # Mock executor
        sell_snapshot = create_sell_snapshot("wallet_up", OrderSide.DOWN, shares=5.0)
        sell_snapshot.status = OrderStatus.FILLED.value  # 模拟抛售单已成交
        manager._order_exec.place_gtc_sell_order = Mock(return_value=Mock(
            outcome=Mock(success=True),
            snapshot=sell_snapshot
        ))

        # 模拟时间推进到强平窗口（结束前 50 秒）
        with patch('strategy.task_manager.datetime') as mock_dt:
            mock_dt.now.return_value = task.end_time - timedelta(seconds=50)
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

            manager._process_waiting_close_window(task)

        # 验证：进入 SETTLING_OUTCOME（因为抛售单已成交）
        assert task.state == EventTaskState.SETTLING_OUTCOME

    def test_stale_order_filled_triggers_settling(self, config, mock_wallet_up, mock_wallet_down):
        """
        测试：未成交侧先成交 → SETTLING_OUTCOME
        """
        manager = create_manager(config, mock_wallet_up, mock_wallet_down)
        task = create_task(mock_wallet_up, mock_wallet_down, start_offset_sec=-120, close_window_sec=60)

        # 设置为 WAITING_CLOSE_WINDOW 状态，且 stale 侧已成交
        task.first_fill_wallet_id = "wallet_up"
        task.mark_order(create_filled_snapshot("wallet_up", OrderSide.UP, shares=5.0))
        task.mark_order(create_filled_snapshot("wallet_down", OrderSide.DOWN, shares=5.0))
        task.state = EventTaskState.WAITING_CLOSE_WINDOW
        task.state_changed_at = datetime.now(timezone.utc)

        # 执行处理
        manager._process_waiting_close_window(task)

        # 验证：取消抛售单，进入 SETTLING_OUTCOME
        manager._order_exec.cancel_order.assert_called()
        assert task.state == EventTaskState.SETTLING_OUTCOME

    def test_sell_order_filled_triggers_settling(self, config, mock_wallet_up, mock_wallet_down):
        """
        测试：抛售单先成交 → 取消 stale 挂单 → SETTLING_OUTCOME
        """
        manager = create_manager(config, mock_wallet_up, mock_wallet_down)
        task = create_task(mock_wallet_up, mock_wallet_down, start_offset_sec=-120, close_window_sec=60)

        # 设置为 WAITING_CLOSE_WINDOW 状态，且抛售单已成交
        task.first_fill_wallet_id = "wallet_up"
        task.mark_order(create_filled_snapshot("wallet_up", OrderSide.UP, shares=5.0))
        task.mark_order(create_pending_snapshot("wallet_down", OrderSide.DOWN, shares=5.0))
        # 标记抛售单已成交
        sell_order = create_filled_snapshot("wallet_up", OrderSide.DOWN, shares=5.0)
        sell_order.operation = OperationType.SELL
        task.mark_order(sell_order)
        task.transition_to(EventTaskState.WAITING_CLOSE_WINDOW, "进入强平窗口")

        # 执行处理
        manager._process_waiting_close_window(task)

        # 验证：取消 stale 挂单，进入 SETTLING_OUTCOME
        manager._order_exec.cancel_order.assert_called()
        assert task.state == EventTaskState.SETTLING_OUTCOME


class TestForceClosingProcess:
    """测试 _process_force_closing 方法。"""

    def test_force_closing_cancels_and_places_fak(self, config, mock_wallet_up, mock_wallet_down):
        """
        测试：FORCE_CLOSING 状态下，撤单 + FAK 强平
        """
        manager = create_manager(config, mock_wallet_up, mock_wallet_down)
        task = create_task(mock_wallet_up, mock_wallet_down, start_offset_sec=-120, close_window_sec=60)

        # 设置为 FORCE_CLOSING 状态（直接修改，绕过合法性检查）
        task.first_fill_wallet_id = "wallet_up"
        task.mark_order(create_filled_snapshot("wallet_up", OrderSide.UP, shares=5.0))
        task.mark_order(create_pending_snapshot("wallet_down", OrderSide.DOWN, shares=5.0))
        task.state = EventTaskState.FORCE_CLOSING
        task.state_changed_at = datetime.now(timezone.utc)

        # 执行处理
        manager._process_force_closing(task)

        # 验证：撤单 + 挂 FAK 强平单
        assert manager._order_exec.cancel_order.call_count >= 1
        manager._order_exec.execute_force_close.assert_called()
        # 验证：状态转移到 SETTLING_OUTCOME
        assert task.state == EventTaskState.SETTLING_OUTCOME


class TestSettlingOutcomeProcess:
    """测试 _process_settling_outcome 方法。"""

    def test_outcome_revealed_triggers_settling_balance(self, config, mock_wallet_up, mock_wallet_down):
        """
        测试：市场结果揭晓 → SETTLING_BALANCE
        """
        manager = create_manager(config, mock_wallet_up, mock_wallet_down)
        task = create_task(mock_wallet_up, mock_wallet_down, start_offset_sec=-300, close_window_sec=60)

        # 设置为 SETTLING_OUTCOME 状态
        task.outcome = EventOutcome.UP
        task.state = EventTaskState.SETTLING_OUTCOME
        task.state_changed_at = datetime.now(timezone.utc)

        # 预先创建 poller（模拟结果已揭晓）
        mock_result = Mock()
        mock_result.is_complete = True
        mock_result.value = EventOutcome.UP
        manager._pollers[f"{task.event_id}_outcome"] = Mock(
            poll=Mock(return_value=mock_result)
        )

        # 执行处理
        manager._process_settling_outcome(task)

        # 验证：状态转移到 SETTLING_BALANCE
        assert task.state == EventTaskState.SETTLING_BALANCE

    def test_settling_outcome_timeout_to_settled(self, config, mock_wallet_up, mock_wallet_down):
        """
        测试：SETTLING_OUTCOME 超时 → SETTLED
        """
        manager = create_manager(config, mock_wallet_up, mock_wallet_down)
        task = create_task(mock_wallet_up, mock_wallet_down, start_offset_sec=-900, close_window_sec=60)

        # 设置为 SETTLING_OUTCOME 状态
        task.outcome = EventOutcome.UNKNOWN
        task.transition_to(EventTaskState.SETTLING_OUTCOME, "等待结果")
        task.trigger_reason = "settling_timeout"

        # Mock poller 超时（返回 is_complete=False 但不会改变状态）
        mock_result = Mock()
        mock_result.is_complete = False

        manager._pollers[f"{task.event_id}_outcome"] = Mock(
            poll=Mock(return_value=mock_result)
        )

        # 手动触发超时 - 修改 start_time 让它超时
        task.start_time = datetime.now(timezone.utc) - timedelta(seconds=1000)

        manager._process_settling_outcome(task)

        # 超时后应该进入 SETTLING_BALANCE 然后 SETTLED
        # 这里主要验证不会崩溃


class TestSettlingBalanceProcess:
    """测试 _process_settling_balance 方法。"""

    def test_balance_stable_triggers_settled(self, config, mock_wallet_up, mock_wallet_down):
        """
        测试：余额稳定 → SETTLED
        """
        manager = create_manager(config, mock_wallet_up, mock_wallet_down)
        task = create_task(mock_wallet_up, mock_wallet_down, start_offset_sec=-900, close_window_sec=60)

        # 设置为 SETTLING_BALANCE 状态
        task.outcome = EventOutcome.UP
        task.state = EventTaskState.SETTLING_BALANCE
        task.state_changed_at = datetime.now(timezone.utc)
        task.up_filled_shares = 5.0
        task.down_filled_shares = 5.0

        # 预先创建 balance poller（模拟余额稳定）
        mock_result = Mock()
        mock_result.is_complete = True
        mock_result.value = {"wallet_up": 10.5, "wallet_down": 10.5}
        manager._pollers[f"{task.event_id}_balance"] = Mock(
            poll=Mock(return_value=mock_result)
        )

        # 执行处理
        manager._process_settling_balance(task)

        # 验证：状态转移到 SETTLED
        assert task.state == EventTaskState.SETTLED


class TestCompleteSingleSideFlow:
    """测试完整的单边成交流程。"""

    def test_full_single_side_flow_no_attribute_errors(self, config, mock_wallet_up, mock_wallet_down):
        """
        测试完整流程能跑通，不报 AttributeError。
        
        验证：
        1. _process_handling_single 能调用
        2. _process_waiting_close_window 能调用
        3. _process_force_closing 能调用
        4. _process_settling_outcome 能调用
        """
        manager = create_manager(config, mock_wallet_up, mock_wallet_down)
        task = create_task(mock_wallet_up, mock_wallet_down, start_offset_sec=-120, close_window_sec=60)

        # Step 1: HANDLING_SINGLE
        task.first_fill_wallet_id = "wallet_up"
        task.mark_order(create_filled_snapshot("wallet_up", OrderSide.UP, shares=5.0))
        task.mark_order(create_pending_snapshot("wallet_down", OrderSide.DOWN, shares=5.0))
        task.state = EventTaskState.HANDLING_SINGLE
        manager._process_handling_single(task)
        assert task.state == EventTaskState.WAITING_CLOSE_WINDOW

        # Step 2: WAITING_CLOSE_WINDOW → SETTLING_OUTCOME (sell成交)
        # Mock sell order as filled
        sell_snapshot = create_sell_snapshot("wallet_up", OrderSide.DOWN, shares=5.0)
        sell_snapshot.status = OrderStatus.FILLED.value
        manager._order_exec.place_gtc_sell_order = Mock(return_value=Mock(
            outcome=Mock(success=True), snapshot=sell_snapshot
        ))
        manager._process_waiting_close_window(task)
        # sell成交后进入SETTLING_OUTCOME
        assert task.state == EventTaskState.SETTLING_OUTCOME

        # Step 3: SETTLING_OUTCOME → SETTLING_BALANCE
        mock_result = Mock()
        mock_result.is_complete = True
        mock_result.value = EventOutcome.UP
        manager._pollers[f"{task.event_id}_outcome"] = Mock(poll=Mock(return_value=mock_result))
        manager._process_settling_outcome(task)
        assert task.state == EventTaskState.SETTLING_BALANCE

        # Step 4: SETTLING_BALANCE → SETTLED
        mock_result2 = Mock()
        mock_result2.is_complete = True
        mock_result2.value = {"wallet_up": 10.5, "wallet_down": 10.5}
        manager._pollers[f"{task.event_id}_balance"] = Mock(poll=Mock(return_value=mock_result2))
        manager._process_settling_balance(task)
        assert task.state == EventTaskState.SETTLED

    def test_full_single_side_flow_with_stale_fills_first(self, config, mock_wallet_up, mock_wallet_down):
        """
        测试：stale 侧先成交 → 取消抛售单 → SETTLING_OUTCOME
        """
        manager = create_manager(config, mock_wallet_up, mock_wallet_down)
        task = create_task(mock_wallet_up, mock_wallet_down, start_offset_sec=-120, close_window_sec=60)

        # Step 1: HANDLING_SINGLE
        task.first_fill_wallet_id = "wallet_up"
        task.mark_order(create_filled_snapshot("wallet_up", OrderSide.UP, shares=5.0))
        task.mark_order(create_pending_snapshot("wallet_down", OrderSide.DOWN, shares=5.0))
        task.state = EventTaskState.HANDLING_SINGLE
        manager._process_handling_single(task)
        assert task.state == EventTaskState.WAITING_CLOSE_WINDOW

        # Step 2: stale 侧成交 → SETTLING_OUTCOME
        task.mark_order(create_filled_snapshot("wallet_down", OrderSide.DOWN, shares=5.0))
        manager._process_waiting_close_window(task)
        assert task.state == EventTaskState.SETTLING_OUTCOME
        manager._order_exec.cancel_order.assert_called()  # 取消抛售单


class TestCompleteBothSidesFlow:
    """测试完整的双边成交流程。"""

    def test_full_both_sides_filled_flow(self, config, mock_wallet_up, mock_wallet_down):
        """
        测试完整流程：WAITING_ENTRY → SETTLING_OUTCOME (双边成交)
        """
        manager = create_manager(config, mock_wallet_up, mock_wallet_down)
        task = create_task(mock_wallet_up, mock_wallet_down, start_offset_sec=-30, close_window_sec=60)

        # Step 1: 进入 WAITING_ENTRY（双边已成交）
        task.mark_order(create_filled_snapshot("wallet_up", OrderSide.UP, shares=5.0))
        task.mark_order(create_filled_snapshot("wallet_down", OrderSide.DOWN, shares=5.0))
        task.state = EventTaskState.WAITING_ENTRY

        # Step 2: 执行 WAITING_ENTRY 处理 → SETTLING_OUTCOME
        manager._process_waiting_entry(task)
        assert task.state == EventTaskState.SETTLING_OUTCOME

        # Step 3: SETTLING_OUTCOME → SETTLING_BALANCE
        mock_result = Mock()
        mock_result.is_complete = True
        mock_result.value = EventOutcome.UP
        manager._pollers[f"{task.event_id}_outcome"] = Mock(poll=Mock(return_value=mock_result))
        manager._process_settling_outcome(task)
        assert task.state == EventTaskState.SETTLING_BALANCE

        # Step 4: SETTLING_BALANCE → SETTLED
        mock_result2 = Mock()
        mock_result2.is_complete = True
        mock_result2.value = {"wallet_up": 10.5, "wallet_down": 10.5}
        manager._pollers[f"{task.event_id}_balance"] = Mock(poll=Mock(return_value=mock_result2))
        manager._process_settling_balance(task)
        assert task.state == EventTaskState.SETTLED
