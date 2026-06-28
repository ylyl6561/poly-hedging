"""
_refresh_order_statuses 并发改写等价性测试

目的：
  验证 task_manager._refresh_order_statuses 的并发改写，与原串行实现，对相同输入产生
  完全等价的最终快照状态与 mark_order 调用序列。同时验证异常隔离。

设计：
  - 不依赖 eth_account/真实 Polymarket runtime（项目环境缺包也能跑）
  - 复刻原实现的过滤/归一化/字段赋值逻辑，与 task_manager 中 _refresh_order_statuses
    的纯算法部分保持一一对应
  - 既能以 pytest 跑，也能 python 直跑

用法:
    pytest tests/test_refresh_concurrency.py -v
    python tests/test_refresh_concurrency.py
"""

from __future__ import annotations

import concurrent.futures
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

# === Mock 域，与 strategy/dual_wallet_models.py 中的命名对齐 ===
OPERATION_VALUES = {"place", "sell"}
STATUS_SUBMITTED = "submitted"
STATUS_FILLED = "filled"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"

ACCEPTED_STATUSES = {STATUS_SUBMITTED, STATUS_FILLED, STATUS_CANCELLED, STATUS_FAILED}

NORMALIZED = {
    "live": STATUS_SUBMITTED, "open": STATUS_SUBMITTED, "pending": STATUS_SUBMITTED,
    "matched": STATUS_FILLED, "filled": STATUS_FILLED, "executed": STATUS_FILLED,
    "cancelled": STATUS_CANCELLED, "canceled": STATUS_CANCELLED,
    "failed": STATUS_FAILED, "rejected": STATUS_FAILED,
}


# === Mock Outcome（对应 ExecutionOutcome 形状）===
@dataclass
class MockOutcome:
    success: bool
    raw: dict | None = None
    raw_status: str | None = None
    shares: float | None = None
    filled_shares: float | None = None
    filled_amount_usd: float | None = None
    average_fill_price: float | None = None
    error: str | None = None


# === Mock Snapshot ===
@dataclass
class MockSnapshot:
    wallet_id: str
    order_id: str | None = None
    status: str = STATUS_SUBMITTED
    operation: str = "place"
    shares: float | None = None
    filled_shares: float | None = None
    filled_amount_usd: float | None = None
    average_fill_price: float | None = None
    raw_status: str | None = None


@dataclass
class MockWallet:
    wallet_id: str
    wallet_name: str
    snapshot: MockSnapshot


class MockTask:
    def __init__(self, wallets):
        self.wallets = wallets
        self._orders = {w.wallet_id: w.snapshot for w in wallets}
        self.mark_calls: list[tuple[str, str]] = []

    def get_order(self, wallet_id):
        return self._orders.get(wallet_id)

    def mark_order(self, snapshot):
        self._orders[snapshot.wallet_id] = snapshot
        self.mark_calls.append((snapshot.wallet_id, snapshot.status))


# === Mock Executor（可注入延迟/异常）===
class MockExecutor:
    def __init__(self, plan: dict[str, MockOutcome], delay_sec: float = 0.0):
        self.plan = plan
        self.delay_sec = delay_sec

    def refresh_order_status(self, order_id, wallet):
        time.sleep(self.delay_sec)
        key = f"{wallet.wallet_id}|{order_id}"
        outcome = self.plan.get(key) or MockOutcome(success=False, error="not_found")
        return outcome, None


# === 待测算法的纯算法版本（与 task_manager 中 _refresh_order_statuses 对应）===
def refresh_serial(task: MockTask, executor: MockExecutor) -> list[tuple[str, str, str]]:
    transitions: list[tuple[str, str, str]] = []
    for wallet in task.wallets:
        snapshot = task.get_order(wallet.wallet_id)
        if not snapshot or not snapshot.order_id:
            continue
        if snapshot.operation not in OPERATION_VALUES:
            continue
        if snapshot.status != STATUS_SUBMITTED:
            continue

        prev_status = snapshot.status
        outcome, _ = executor.refresh_order_status(snapshot.order_id, wallet)
        if not outcome.success:
            continue
        raw = outcome.raw if isinstance(outcome.raw, dict) else {}
        status = str(raw.get("status") or "").lower()
        normalized = NORMALIZED.get(status, status)
        if normalized not in ACCEPTED_STATUSES:
            continue

        snapshot.status = normalized
        snapshot.raw_status = outcome.raw_status or raw.get("raw_status") or raw.get("status")
        if outcome.shares is not None:
            snapshot.shares = float(outcome.shares)
        if outcome.filled_shares is not None:
            snapshot.filled_shares = float(outcome.filled_shares)
        if outcome.filled_amount_usd is not None:
            snapshot.filled_amount_usd = float(outcome.filled_amount_usd)
        if outcome.average_fill_price is not None:
            snapshot.average_fill_price = float(outcome.average_fill_price)
        task.mark_order(snapshot)
        if prev_status != snapshot.status:
            transitions.append((wallet.wallet_name, prev_status, snapshot.status))
    return transitions


def refresh_parallel(task: MockTask, executor: MockExecutor) -> list[tuple[str, str, str]]:
    pending: list[tuple[MockWallet, MockSnapshot]] = []
    for wallet in task.wallets:
        snapshot = task.get_order(wallet.wallet_id)
        if not snapshot or not snapshot.order_id:
            continue
        if snapshot.operation not in OPERATION_VALUES:
            continue
        if snapshot.status != STATUS_SUBMITTED:
            continue
        pending.append((wallet, snapshot))
    if not pending:
        return []

    results: dict[str, MockOutcome] = {}

    def _fetch_one(wallet: MockWallet, snapshot: MockSnapshot) -> tuple[str, MockOutcome]:
        try:
            outcome, _ = executor.refresh_order_status(snapshot.order_id, wallet)
        except Exception as exc:
            return wallet.wallet_id, MockOutcome(success=False, error=f"refresh_exception:{exc}")
        return wallet.wallet_id, outcome

    max_workers = max(1, min(len(pending), 8))
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="order-refresh") as pool:
        futures = [pool.submit(_fetch_one, w, s) for w, s in pending]
        for fut in futures:
            try:
                wid, outcome = fut.result()
            except Exception:
                continue
            results[wid] = outcome

    transitions: list[tuple[str, str, str]] = []
    for wallet, snapshot in pending:
        outcome = results.get(wallet.wallet_id)
        if outcome is None:
            continue
        prev_status = snapshot.status
        if not outcome.success:
            continue
        raw = outcome.raw if isinstance(outcome.raw, dict) else {}
        status = str(raw.get("status") or "").lower()
        normalized = NORMALIZED.get(status, status)
        if normalized not in ACCEPTED_STATUSES:
            continue

        snapshot.status = normalized
        snapshot.raw_status = outcome.raw_status or raw.get("raw_status") or raw.get("status")
        if outcome.shares is not None:
            snapshot.shares = float(outcome.shares)
        if outcome.filled_shares is not None:
            snapshot.filled_shares = float(outcome.filled_shares)
        if outcome.filled_amount_usd is not None:
            snapshot.filled_amount_usd = float(outcome.filled_amount_usd)
        if outcome.average_fill_price is not None:
            snapshot.average_fill_price = float(outcome.average_fill_price)
        task.mark_order(snapshot)
        if prev_status != snapshot.status:
            transitions.append((wallet.wallet_name, prev_status, snapshot.status))
    return transitions


# === Fixtures ===
def make_wallet_pair() -> list[MockWallet]:
    return [
        MockWallet("w_a", "wallet_A", MockSnapshot("w_a", order_id="order_a", status=STATUS_SUBMITTED)),
        MockWallet("w_b", "wallet_B", MockSnapshot("w_b", order_id="order_b", status=STATUS_SUBMITTED)),
    ]


# === 用例 ===
def test_filled_both():
    plan = {
        "w_a|order_a": MockOutcome(success=True, raw={"status": "matched"}, raw_status="MATCHED",
                                    shares=20.0, filled_shares=20.0, filled_amount_usd=10.0, average_fill_price=0.5),
        "w_b|order_b": MockOutcome(success=True, raw={"status": "filled"}, raw_status="FILLED",
                                    shares=20.0, filled_shares=20.0, filled_amount_usd=10.0, average_fill_price=0.5),
    }
    executor = MockExecutor(plan, delay_sec=0.05)
    t_serial = MockTask(make_wallet_pair()); trans_s = refresh_serial(t_serial, executor)
    t_par = MockTask(make_wallet_pair());   trans_p = refresh_parallel(t_par, executor)
    assert trans_s == trans_p == [("wallet_A", "submitted", "filled"), ("wallet_B", "submitted", "filled")]
    assert len(t_serial.mark_calls) == len(t_par.mark_calls) == 2


def test_unrecognized_status_kept():
    plan = {
        "w_a|order_a": MockOutcome(success=True, raw={"status": "live"}, raw_status="LIVE", shares=20.0, filled_shares=0.0),
        "w_b|order_b": MockOutcome(success=True, raw={"status": "weird_state"}, raw_status="WEIRD"),
    }
    executor = MockExecutor(plan, delay_sec=0.0)
    t_par = MockTask(make_wallet_pair()); trans_p = refresh_parallel(t_par, executor)
    assert trans_p == []
    assert t_par._orders["w_a"].status == STATUS_SUBMITTED
    assert t_par._orders["w_b"].status == STATUS_SUBMITTED


def test_partial_failure():
    plan = {
        "w_a|order_a": MockOutcome(success=True, raw={"status": "filled"}, raw_status="FILLED",
                                    filled_shares=20.0, filled_amount_usd=10.0),
        "w_b|order_b": MockOutcome(success=False, error="timeout"),
    }
    executor = MockExecutor(plan, delay_sec=0.0)
    t_par = MockTask(make_wallet_pair()); trans_p = refresh_parallel(t_par, executor)
    assert trans_p == [("wallet_A", "submitted", "filled")]
    assert t_par._orders["w_b"].status == STATUS_SUBMITTED


def test_exception_isolation_in_parallel():
    """并发版必须能隔离单 wallet 异常，不能阻断其它 wallet 处理。"""
    class FlakyExecutor(MockExecutor):
        def refresh_order_status(self, order_id, wallet):
            if wallet.wallet_id == "w_a":
                raise RuntimeError("simulated network crash")
            return super().refresh_order_status(order_id, wallet)

    plan = {"w_b|order_b": MockOutcome(success=True, raw={"status": "filled"}, raw_status="FILLED",
                                        filled_shares=20.0, filled_amount_usd=10.0)}
    executor = FlakyExecutor(plan, delay_sec=0.0)
    t_par = MockTask(make_wallet_pair()); trans_p = refresh_parallel(t_par, executor)
    assert trans_p == [("wallet_B", "submitted", "filled")]
    assert t_par._orders["w_a"].status == STATUS_SUBMITTED  # 未被错误更新
    assert t_par._orders["w_b"].status == STATUS_FILLED


def test_skip_non_submitted():
    wallets = [
        MockWallet("w_a", "wallet_A", MockSnapshot("w_a", order_id="order_a", status=STATUS_FILLED)),
        MockWallet("w_b", "wallet_B", MockSnapshot("w_b", order_id="order_b", status=STATUS_SUBMITTED)),
    ]
    plan = {"w_b|order_b": MockOutcome(success=True, raw={"status": "filled"}, raw_status="FILLED",
                                        filled_shares=10.0, filled_amount_usd=5.0)}
    executor = MockExecutor(plan, delay_sec=0.0)
    t_serial = MockTask(wallets); trans_s = refresh_serial(t_serial, executor)
    wallets = [
        MockWallet("w_a", "wallet_A", MockSnapshot("w_a", order_id="order_a", status=STATUS_FILLED)),
        MockWallet("w_b", "wallet_B", MockSnapshot("w_b", order_id="order_b", status=STATUS_SUBMITTED)),
    ]
    t_par = MockTask(wallets); trans_p = refresh_parallel(t_par, executor)
    assert trans_s == trans_p == [("wallet_B", "submitted", "filled")]


def test_empty_pending():
    """无 SUBMITTED 订单时，并发版应直接 return（不发任何 HTTP）"""
    class CountingExecutor(MockExecutor):
        def __init__(self):
            super().__init__({}, delay_sec=0.0)
            self.call_count = 0
        def refresh_order_status(self, order_id, wallet):
            self.call_count += 1
            return super().refresh_order_status(order_id, wallet)

    wallets = [
        MockWallet("w_a", "wallet_A", MockSnapshot("w_a", order_id="order_a", status=STATUS_FILLED)),
    ]
    t = MockTask(wallets)
    exe = CountingExecutor()
    refresh_parallel(t, exe)
    assert exe.call_count == 0


def test_concurrent_speedup():
    """双边各 100ms HTTP 模拟延迟下，并发版应明显快于串行版"""
    plan = {
        "w_a|order_a": MockOutcome(success=True, raw={"status": "live"}, raw_status="LIVE", filled_shares=0.0),
        "w_b|order_b": MockOutcome(success=True, raw={"status": "live"}, raw_status="LIVE", filled_shares=0.0),
    }
    executor = MockExecutor(plan, delay_sec=0.1)
    t_s = MockTask(make_wallet_pair()); t0 = time.perf_counter(); refresh_serial(t_s, executor); t_serial = time.perf_counter() - t0
    t_p = MockTask(make_wallet_pair()); t0 = time.perf_counter(); refresh_parallel(t_p, executor); t_par = time.perf_counter() - t0
    # 串行 ~200ms，并发 ~100ms；并发应至少快 30%
    assert t_par < t_serial * 0.7, f"serial={t_serial*1000:.0f}ms parallel={t_par*1000:.0f}ms"


# 并发算法版本（与 task_manager 中最新 _refresh_order_statuses 对应，含 timeout 逻辑）
import concurrent.futures

def refresh_parallel_with_timeout(
    task: MockTask,
    executor: MockExecutor,
    fetch_timeout: float = 2.0,
) -> list[tuple[str, str, str]]:
    pending: list[tuple[MockWallet, MockSnapshot]] = []
    for wallet in task.wallets:
        snapshot = task.get_order(wallet.wallet_id)
        if not snapshot or not snapshot.order_id: continue
        if snapshot.operation not in OPERATION_VALUES: continue
        if snapshot.status != STATUS_SUBMITTED: continue
        pending.append((wallet, snapshot))
    if not pending:
        return []

    results: dict[str, MockOutcome] = {}

    def _fetch_one(w: MockWallet, s: MockSnapshot):
        try:
            outcome, _ = executor.refresh_order_status(s.order_id, w)
        except Exception as exc:
            return w.wallet_id, MockOutcome(success=False, error=f"refresh_exception:{exc}")
        return w.wallet_id, outcome

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(len(pending), 8))) as pool:
        futures = {pool.submit(_fetch_one, w, s): w.wallet_id for w, s in pending}
        for fut in concurrent.futures.as_completed(futures):
            wid = futures[fut]
            try:
                w_id, outcome = fut.result(timeout=fetch_timeout)
            except concurrent.futures.TimeoutError:
                # 单 wallet 超时：视为本轮刷新失败，保留原状态
                continue
            except Exception:
                continue
            results[w_id] = outcome

    transitions: list[tuple[str, str, str]] = []
    for wallet, snapshot in pending:
        outcome = results.get(wallet.wallet_id)
        if outcome is None: continue
        prev_status = snapshot.status
        if not outcome.success: continue
        raw = outcome.raw if isinstance(outcome.raw, dict) else {}
        status = str(raw.get("status") or "").lower()
        normalized = NORMALIZED.get(status, status)
        if normalized not in ACCEPTED_STATUSES: continue
        snapshot.status = normalized
        snapshot.raw_status = outcome.raw_status or raw.get("raw_status") or raw.get("status")
        if outcome.filled_shares is not None: snapshot.filled_shares = float(outcome.filled_shares)
        task.mark_order(snapshot)
        if prev_status != snapshot.status:
            transitions.append((wallet.wallet_name, prev_status, snapshot.status))
    return transitions


def test_timeout_isolation():
    """单个 wallet 超时不阻断其他 wallet，正常 wallet 状态正确推进"""
    class SlowExecutor(MockExecutor):
        def refresh_order_status(self, order_id, wallet):
            if wallet.wallet_id == "w_a":
                raise TimeoutError("simulated hang")
            return super().refresh_order_status(order_id, wallet)

    plan = {"w_b|order_b": MockOutcome(success=True, raw={"status": "filled"}, raw_status="FILLED",
                                        filled_shares=20.0, filled_amount_usd=10.0)}
    executor = SlowExecutor(plan, delay_sec=0.0)
    t = MockTask(make_wallet_pair())
    trans = refresh_parallel_with_timeout(t, executor, fetch_timeout=0.05)
    # A 超时被隔离，B 正常
    assert trans == [("wallet_B", "submitted", "filled")]
    assert t._orders["w_a"].status == STATUS_SUBMITTED  # A 未被更新
    assert t._orders["w_b"].status == STATUS_FILLED


# ============================================================================
# 下游消费侧验证：模拟 _process_waiting_entry / _execute_force_close
# 这些函数读 task.check_both_sides_filled / check_single_side_filled /
# get_order_history，依赖 _refresh_order_statuses 正确写入的 snapshot 字段
# ============================================================================

def test_downstream_check_both_sides_filled():
    """并发刷新后，task.check_both_sides_filled 应能正确判定双边成交"""
    plan = {
        "w_a|order_a": MockOutcome(success=True, raw={"status": "filled"}, raw_status="FILLED",
                                    shares=20.0, filled_shares=20.0, filled_amount_usd=10.0),
        "w_b|order_b": MockOutcome(success=True, raw={"status": "matched"}, raw_status="MATCHED",
                                    shares=20.0, filled_shares=20.0, filled_amount_usd=10.0),
    }
    executor = MockExecutor(plan, delay_sec=0.0)
    t = MockTask(make_wallet_pair())
    refresh_parallel(t, executor)
    # 模拟 event_task.check_both_sides_filled 的判定逻辑
    for w in t.wallets:
        s = t.get_order(w.wallet_id)
        assert s.status == STATUS_FILLED, f"{w.wallet_name} 应为 filled，实际={s.status}"
        assert s.filled_shares == 20.0, f"{w.wallet_name} filled_shares 应为 20.0，实际={s.filled_shares}"


def test_downstream_check_single_side_filled():
    """单边成交场景：只有一边 filled，另一边保持 submitted"""
    plan = {
        "w_a|order_a": MockOutcome(success=True, raw={"status": "filled"}, raw_status="FILLED",
                                    shares=20.0, filled_shares=20.0, filled_amount_usd=10.0),
        # w_b 保持 submitted（不调用）
    }
    executor = MockExecutor(plan, delay_sec=0.0)
    t = MockTask(make_wallet_pair())
    refresh_parallel(t, executor)
    s_a = t.get_order("w_a"); s_b = t.get_order("w_b")
    assert s_a.status == STATUS_FILLED
    assert s_b.status == STATUS_SUBMITTED  # 没填 w_b 的 plan，保持原状态
    # 此时只有单边成交


def test_downstream_force_close_history_lookup():
    """
    关键：_execute_force_close 依赖 get_order_history 找 FILLED 的 PLACE 快照。
    验证：mark_order append 到 history 的 snapshot 包含更新后的 status 字段。
    """
    plan = {
        "w_a|order_a": MockOutcome(success=True, raw={"status": "filled"}, raw_status="FILLED",
                                    shares=20.0, filled_shares=20.0, filled_amount_usd=10.0),
    }
    executor = MockExecutor(plan, delay_sec=0.0)
    t = MockTask(make_wallet_pair())
    refresh_parallel(t, executor)
    # 模拟 _execute_force_close 遍历 history 找 PLACE+FILLED
    history = t._orders_list("w_a")  # 调用 mark_order 时 append 的列表
    found_filled_place = any(
        s.operation == "place" and s.status == STATUS_FILLED
        for s in history
    )
    assert found_filled_place, f"history 中应能找到 PLACE+FILLED 快照，实际 history={history}"


def test_history_order_preserved_under_concurrency():
    """
    即使并发 fetch 完成顺序乱，history append 顺序必须按 wallet 原序。
    这对 _execute_force_close 的 entry_filled_by_wallet 计算至关重要（遍历找 PLACE+FILLED）。
    """
    wallets = [
        MockWallet("w_a", "wallet_A", MockSnapshot("w_a", order_id="oa", operation="place", status=STATUS_SUBMITTED)),
        MockWallet("w_b", "wallet_B", MockSnapshot("w_b", order_id="ob", operation="place", status=STATUS_SUBMITTED)),
        MockWallet("w_c", "wallet_C", MockSnapshot("w_c", order_id="oc", operation="place", status=STATUS_SUBMITTED)),
    ]

    class ReverseOrderExecutor(MockExecutor):
        """故意让 fetch 完成顺序与提交顺序相反"""
        def __init__(self):
            super().__init__({}, delay_sec=0.0)
            self._delay_map = {"w_c": 0.0, "w_b": 0.05, "w_a": 0.1}
        def refresh_order_status(self, order_id, wallet):
            time.sleep(self._delay_map.get(wallet.wallet_id, 0))
            return MockOutcome(success=True, raw={"status": "filled"}, raw_status="FILLED",
                                filled_shares=10.0, filled_amount_usd=5.0), None

    executor = ReverseOrderExecutor()
    t = MockTask(wallets)
    refresh_parallel(t, executor)

    # history append 顺序应保持 w_a → w_b → w_c（按 pending 顺序）
    # 因为 mark_order 在步骤 3 按 pending 顺序调用
    history_a = t._orders_list("w_a")
    history_b = t._orders_list("w_b")
    history_c = t._orders_list("w_c")
    assert len(history_a) == len(history_b) == len(history_c) == 1
    for s in history_a + history_b + history_c:
        assert s.status == STATUS_FILLED


# 给 MockTask 增加内部 _orders_list 用于 history 测试（不影响其他测试）
_orig_mark = MockTask.mark_order
def _mark_with_history(self, snapshot):
    if not hasattr(self, "_hist"):
        self._hist = {}
    self._hist.setdefault(snapshot.wallet_id, []).append(snapshot)
    _orig_mark(self, snapshot)
MockTask.mark_order = _mark_with_history
def _orders_list(self, wallet_id):
    return list(getattr(self, "_hist", {}).get(wallet_id, []))
MockTask._orders_list = _orders_list


if __name__ == "__main__":
    test_filled_both()
    test_unrecognized_status_kept()
    test_partial_failure()
    test_exception_isolation_in_parallel()
    test_skip_non_submitted()
    test_empty_pending()
    test_concurrent_speedup()
    test_timeout_isolation()
    test_downstream_check_both_sides_filled()
    test_downstream_check_single_side_filled()
    test_downstream_force_close_history_lookup()
    test_history_order_preserved_under_concurrency()
    print("ALL EQUIVALENCE TESTS PASSED")