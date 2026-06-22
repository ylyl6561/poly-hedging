"""
DualWalletModels 单元测试

测试 dual_wallet_models 模块中的所有核心功能：
- 枚举类型 (WalletRole, OrderSide, OrderStatus, etc.)
- 数据类 (WalletIdentity, OrderSnapshot, EventResultSummary, etc.)
- 辅助函数 (build_wallet_identities, assign_event_sides, etc.)
"""

import pytest
from datetime import datetime, timezone, timedelta
from dataclasses import is_dataclass, asdict

from strategy.dual_wallet_models import (
    # 枚举
    WalletRole,
    OrderSide,
    OperationType,
    EventOutcome,
    EventFlowState,
    OrderStatus,
    # 数据类
    WalletIdentity,
    OrderSnapshot,
    EventResultSummary,
    LossWindowTracker,
    DualWalletEventState,
    # 辅助函数
    build_wallet_identities,
    assign_event_sides,
    format_operation_timestamp,
)


class MockAccountContext:
    """模拟 AccountContext 用于测试。"""
    def __init__(self, account_id: str = "test_account", label: str = "Test Wallet"):
        self.account_id = account_id
        self.label = label


class TestEnums:
    """测试枚举类型。"""

    def test_wallet_role_values(self):
        assert WalletRole.A.value == "a"
        assert WalletRole.B.value == "b"

    def test_order_side_values(self):
        assert OrderSide.UP.value == "UP"
        assert OrderSide.DOWN.value == "DOWN"

    def test_order_side_token_index(self):
        assert OrderSide.UP.token_index == 0
        assert OrderSide.DOWN.token_index == 1

    def test_order_side_from_string(self):
        assert OrderSide.from_string("up") == OrderSide.UP
        assert OrderSide.from_string("UP") == OrderSide.UP
        assert OrderSide.from_string("yes") == OrderSide.UP
        assert OrderSide.from_string("down") == OrderSide.DOWN
        assert OrderSide.from_string("DOWN") == OrderSide.DOWN
        assert OrderSide.from_string("no") == OrderSide.DOWN
        with pytest.raises(ValueError):
            OrderSide.from_string("invalid")

    def test_order_side_to_api_str(self):
        assert OrderSide.UP.to_api_str() == "yes"
        assert OrderSide.DOWN.to_api_str() == "no"

    def test_operation_type_values(self):
        assert OperationType.PLACE.value == "挂单"
        assert OperationType.CANCEL.value == "取消挂单"
        assert OperationType.SELL.value == "挂卖"
        assert OperationType.FORCE_CLOSE.value == "平仓"

    def test_event_outcome_values(self):
        assert EventOutcome.UP.value == "UP"
        assert EventOutcome.DOWN.value == "DOWN"
        assert EventOutcome.UNKNOWN.value == "UNKNOWN"

    def test_event_flow_state_values(self):
        assert EventFlowState.NEW.value == "new"
        assert EventFlowState.ENTRY_PLACED.value == "entry_placed"
        assert EventFlowState.SETTLED.value == "settled"

    def test_order_status_values(self):
        assert OrderStatus.SUBMITTED.value == "submitted"
        assert OrderStatus.FILLED.value == "filled"
        assert OrderStatus.CANCELLED.value == "cancelled"
        assert OrderStatus.FAILED.value == "failed"
        assert OrderStatus.CLOSED.value == "closed"


class TestWalletIdentity:
    """测试 WalletIdentity 数据类。"""

    def test_is_dataclass(self):
        assert is_dataclass(WalletIdentity)

    def test_frozen(self):
        account = MockAccountContext()
        wallet = WalletIdentity(
            wallet_id="w1",
            wallet_name="Wallet A",
            role=WalletRole.A,
            account=account,
        )
        # frozen=True 的 dataclass 不可修改
        with pytest.raises(Exception):  # dataclasses.FrozenInstanceError
            wallet.wallet_id = "w2"

    def test_creation(self):
        account = MockAccountContext()
        wallet = WalletIdentity(
            wallet_id="w1",
            wallet_name="Test Wallet",
            role=WalletRole.A,
            account=account,
        )
        assert wallet.wallet_id == "w1"
        assert wallet.wallet_name == "Test Wallet"
        assert wallet.role == WalletRole.A
        assert wallet.account == account


class TestOrderSnapshot:
    """测试 OrderSnapshot 数据类。"""

    def test_is_dataclass(self):
        assert is_dataclass(OrderSnapshot)

    def test_creation_with_defaults(self):
        wallet = WalletIdentity(
            wallet_id="w1",
            wallet_name="Test",
            role=WalletRole.A,
            account=MockAccountContext(),
        )
        snapshot = OrderSnapshot(
            wallet=wallet,
            event_name="BTC > 100k",
            side=OrderSide.UP,
            amount_usd=10.0,
            operation=OperationType.PLACE,
        )
        assert snapshot.wallet == wallet
        assert snapshot.event_name == "BTC > 100k"
        assert snapshot.side == OrderSide.UP
        assert snapshot.amount_usd == 10.0
        assert snapshot.operation == OperationType.PLACE
        assert snapshot.timestamp is not None
        assert snapshot.order_id is None
        assert snapshot.status is None

    def test_timestamp_cn(self):
        wallet = WalletIdentity(
            wallet_id="w1",
            wallet_name="Test",
            role=WalletRole.A,
            account=MockAccountContext(),
        )
        # 使用 UTC 时间，转换到北京时间会显示为次日
        snapshot = OrderSnapshot(
            wallet=wallet,
            event_name="test",
            side=OrderSide.UP,
            amount_usd=10.0,
            operation=OperationType.PLACE,
            timestamp=datetime(2026, 6, 20, 22, 30, 0, tzinfo=timezone.utc),
        )
        cn = snapshot.timestamp_cn
        # 北京时间 = UTC + 8 小时，所以 22:30 UTC = 次日 06:30 北京时间
        assert "06月21日" in cn
        assert "06:30:00" in cn

    def test_asdict(self):
        wallet = WalletIdentity(
            wallet_id="w1",
            wallet_name="Test",
            role=WalletRole.A,
            account=MockAccountContext(),
        )
        snapshot = OrderSnapshot(
            wallet=wallet,
            event_name="test",
            side=OrderSide.UP,
            amount_usd=10.0,
            operation=OperationType.PLACE,
        )
        d = asdict(snapshot)
        assert isinstance(d, dict)
        # asdict() 会递归转换嵌套 dataclass 为 dict
        assert d["wallet"]["wallet_id"] == "w1"
        assert d["wallet"]["wallet_name"] == "Test"
        assert d["event_name"] == "test"


class TestEventResultSummary:
    """测试 EventResultSummary 数据类。"""

    def test_is_dataclass(self):
        assert is_dataclass(EventResultSummary)

    def test_creation_with_defaults(self):
        summary = EventResultSummary(event_name="test")
        assert summary.event_name == "test"
        assert summary.outcome == EventOutcome.UNKNOWN
        assert summary.total_pnl_usd == 0.0
        assert summary.wallet_pnl_usd == {}
        assert summary.is_profit is False
        assert summary.settled_at is None

    def test_creation_full(self):
        now = datetime.now(timezone.utc)
        summary = EventResultSummary(
            event_name="BTC event",
            outcome=EventOutcome.UP,
            total_pnl_usd=1.5,
            wallet_pnl_usd={"w1": 1.0, "w2": 0.5},
            wallet_balance_usdc={"w1": 100.0, "w2": 200.0},
            order_count=4,
            filled_count=2,
            cancelled_count=1,
            force_closed_count=1,
            is_profit=True,
            settled_at=now,
        )
        assert summary.event_name == "BTC event"
        assert summary.outcome == EventOutcome.UP
        assert summary.total_pnl_usd == 1.5
        assert summary.wallet_pnl_usd == {"w1": 1.0, "w2": 0.5}
        assert summary.is_profit is True
        assert summary.settled_at == now

    def test_asdict(self):
        summary = EventResultSummary(event_name="test")
        d = asdict(summary)
        assert isinstance(d, dict)
        assert d["event_name"] == "test"


class TestLossWindowTracker:
    """测试 LossWindowTracker 数据类。"""

    def test_record_and_consecutive_losses(self):
        tracker = LossWindowTracker()
        assert tracker.consecutive_losses() == 0

        tracker.record(True)
        assert tracker.consecutive_losses() == 0

        tracker.record(False)
        assert tracker.consecutive_losses() == 1

        tracker.record(False)
        assert tracker.consecutive_losses() == 2

        tracker.record(True)
        assert tracker.consecutive_losses() == 0

    def test_should_halt(self):
        tracker = LossWindowTracker()
        assert tracker.should_halt(0) is False
        assert tracker.should_halt(1) is False

        tracker.record(False)
        assert tracker.should_halt(1) is True
        assert tracker.should_halt(2) is False

        tracker.record(False)
        assert tracker.should_halt(2) is True

    def test_maxlen(self):
        tracker = LossWindowTracker()
        for _ in range(10):
            tracker.record(False)
        # maxlen=5，所以最多只有5个连续亏损记录
        assert tracker.consecutive_losses() == 5


class TestDualWalletEventState:
    """测试 DualWalletEventState 数据类。"""

    def test_is_dataclass(self):
        assert is_dataclass(DualWalletEventState)

    def test_creation(self):
        now = datetime.now(timezone.utc)
        end = now + timedelta(hours=1)
        state = DualWalletEventState(
            event_name="test",
            event_id="cond123",
            start_time=now,
            end_time=end,
            close_price=0.5,
            close_window_sec=60,
            x_timeout_sec=100,
        )
        assert state.event_name == "test"
        assert state.flow_state == EventFlowState.NEW
        assert state.wallet_orders == {}
        assert state.first_fill_wallet_id is None

    def test_mark_order(self):
        wallet = WalletIdentity(
            wallet_id="w1",
            wallet_name="Test",
            role=WalletRole.A,
            account=MockAccountContext(),
        )
        snapshot = OrderSnapshot(
            wallet=wallet,
            event_name="test",
            side=OrderSide.UP,
            amount_usd=10.0,
            operation=OperationType.PLACE,
            status=OrderStatus.FILLED.value,
        )
        state = DualWalletEventState(
            event_name="test",
            event_id="cond123",
            start_time=datetime.now(timezone.utc),
            end_time=datetime.now(timezone.utc) + timedelta(hours=1),
            close_price=0.5,
            close_window_sec=60,
            x_timeout_sec=100,
        )
        state.mark_order(snapshot)

        assert state.wallet_orders["w1"] == snapshot
        assert state.wallet_order_history["w1"] == [snapshot]
        assert state.wallet_status["w1"] == OrderStatus.FILLED.value

    def test_count_filled(self):
        wallet_a = WalletIdentity(
            wallet_id="w1",
            wallet_name="A",
            role=WalletRole.A,
            account=MockAccountContext("w1"),
        )
        wallet_b = WalletIdentity(
            wallet_id="w2",
            wallet_name="B",
            role=WalletRole.B,
            account=MockAccountContext("w2"),
        )

        state = DualWalletEventState(
            event_name="test",
            event_id="cond123",
            start_time=datetime.now(timezone.utc),
            end_time=datetime.now(timezone.utc) + timedelta(hours=1),
            close_price=0.5,
            close_window_sec=60,
            x_timeout_sec=100,
        )
        assert state.count_filled() == 0

        state.mark_order(OrderSnapshot(
            wallet=wallet_a,
            event_name="test",
            side=OrderSide.UP,
            amount_usd=10.0,
            operation=OperationType.PLACE,
            status=OrderStatus.FILLED.value,
        ))
        assert state.count_filled() == 1

        state.mark_order(OrderSnapshot(
            wallet=wallet_b,
            event_name="test",
            side=OrderSide.DOWN,
            amount_usd=10.0,
            operation=OperationType.PLACE,
            status=OrderStatus.FILLED.value,
        ))
        assert state.count_filled() == 2

    def test_active_wallet_ids(self):
        wallet_a = WalletIdentity(
            wallet_id="w1",
            wallet_name="A",
            role=WalletRole.A,
            account=MockAccountContext("w1"),
        )
        wallet_b = WalletIdentity(
            wallet_id="w2",
            wallet_name="B",
            role=WalletRole.B,
            account=MockAccountContext("w2"),
        )

        state = DualWalletEventState(
            event_name="test",
            event_id="cond123",
            start_time=datetime.now(timezone.utc),
            end_time=datetime.now(timezone.utc) + timedelta(hours=1),
            close_price=0.5,
            close_window_sec=60,
            x_timeout_sec=100,
        )
        state.mark_order(OrderSnapshot(
            wallet=wallet_a,
            event_name="test",
            side=OrderSide.UP,
            amount_usd=10.0,
            operation=OperationType.PLACE,
            status=OrderStatus.FILLED.value,
        ))
        state.mark_order(OrderSnapshot(
            wallet=wallet_b,
            event_name="test",
            side=OrderSide.DOWN,
            amount_usd=10.0,
            operation=OperationType.CANCEL,
            status=OrderStatus.CANCELLED.value,
        ))

        active = state.active_wallet_ids()
        assert "w1" in active  # filled 是活跃的
        assert "w2" not in active  # cancelled 不是活跃的

    def test_time_calculations(self):
        now = datetime.now(timezone.utc)
        start = now - timedelta(minutes=30)
        end = now + timedelta(minutes=30)

        state = DualWalletEventState(
            event_name="test",
            event_id="cond123",
            start_time=start,
            end_time=end,
            close_price=0.5,
            close_window_sec=60,
            x_timeout_sec=100,
        )

        # 剩余到开始应该是负数（已过去）
        remaining_start = state.remaining_to_start(now)
        assert remaining_start < 0

        # 剩余到结束应该是正数
        remaining_end = state.remaining_to_end(now)
        assert 0 < remaining_end < 3600  # 约30分钟

        # 强平窗口检查
        assert state.is_within_force_close_window(now) is False  # 距结束30分钟 > 60秒

        # 距结束50秒时应该在强平窗口
        near_end = end - timedelta(seconds=50)
        assert state.is_within_force_close_window(near_end) is True


class TestBuildWalletIdentities:
    """测试 build_wallet_identities 函数。"""

    def test_requires_two_accounts(self):
        with pytest.raises(ValueError, match="at least two accounts"):
            build_wallet_identities([])

        with pytest.raises(ValueError, match="at least two accounts"):
            build_wallet_identities([MockAccountContext("w1")])

    def test_creates_two_wallets(self):
        accounts = [
            MockAccountContext("acc1", "Wallet A"),
            MockAccountContext("acc2", "Wallet B"),
        ]
        wallets = build_wallet_identities(accounts)

        assert len(wallets) == 2
        assert wallets[0].wallet_id == "acc1"
        assert wallets[0].wallet_name == "Wallet A"
        assert wallets[0].role == WalletRole.A
        assert wallets[1].wallet_id == "acc2"
        assert wallets[1].wallet_name == "Wallet B"
        assert wallets[1].role == WalletRole.B

    def test_uses_first_two_accounts(self):
        accounts = [
            MockAccountContext("acc1", "First"),
            MockAccountContext("acc2", "Second"),
            MockAccountContext("acc3", "Third"),
        ]
        wallets = build_wallet_identities(accounts)
        assert wallets[0].wallet_id == "acc1"
        assert wallets[1].wallet_id == "acc2"


class TestAssignEventSides:
    """测试 assign_event_sides 函数。"""

    def test_requires_exactly_two_wallets(self):
        wallet = WalletIdentity(
            wallet_id="w1",
            wallet_name="Test",
            role=WalletRole.A,
            account=MockAccountContext(),
        )
        with pytest.raises(ValueError, match="exactly two wallets"):
            assign_event_sides([wallet])

        with pytest.raises(ValueError, match="exactly two wallets"):
            assign_event_sides([])

    def test_assigns_opposite_sides(self):
        wallets = [
            WalletIdentity(
                wallet_id="w1",
                wallet_name="A",
                role=WalletRole.A,
                account=MockAccountContext("w1"),
            ),
            WalletIdentity(
                wallet_id="w2",
                wallet_name="B",
                role=WalletRole.B,
                account=MockAccountContext("w2"),
            ),
        ]

        mapping, first_is_up = assign_event_sides(wallets)

        assert len(mapping) == 2
        assert mapping["w1"] != mapping["w2"]  # 必须不同
        assert set(mapping.values()) == {OrderSide.UP, OrderSide.DOWN}

        if first_is_up:
            assert mapping["w1"] == OrderSide.UP
            assert mapping["w2"] == OrderSide.DOWN
        else:
            assert mapping["w1"] == OrderSide.DOWN
            assert mapping["w2"] == OrderSide.UP

    def test_randomness(self):
        wallets = [
            WalletIdentity(
                wallet_id="w1",
                wallet_name="A",
                role=WalletRole.A,
                account=MockAccountContext("w1"),
            ),
            WalletIdentity(
                wallet_id="w2",
                wallet_name="B",
                role=WalletRole.B,
                account=MockAccountContext("w2"),
            ),
        ]

        results = set()
        for _ in range(20):
            _, first_is_up = assign_event_sides(wallets)
            results.add(first_is_up)

        # 随机性测试：20次中应该有两种结果
        assert len(results) == 2


class TestFormatOperationTimestamp:
    """测试 format_operation_timestamp 函数。"""

    def test_with_none(self):
        result = format_operation_timestamp()
        assert isinstance(result, str)
        assert "月" in result
        assert "日" in result
        # 格式是 %m月%d日 %H:%M:%S，没有"时"字
        assert ":" in result

    def test_with_specific_time(self):
        dt = datetime(2026, 6, 20, 15, 30, 45)
        result = format_operation_timestamp(dt)
        assert "06月20日" in result or "6月20日" in result
        assert "15:30:45" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
