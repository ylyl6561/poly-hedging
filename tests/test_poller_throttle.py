"""
Poller 节流行为测试。

验证：
1. tick 频率 100ms 时，_check() 调用频率仍按 poll_interval_sec 节流
2. is_timeout 判定不被节流（task 能在 deadline 到达时及时退出）
3. is_complete 状态不受节流影响
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy.pollers import Poller


class CounterPoller(Poller):
    """简单计数 poller，用于验证 _check() 调用次数"""
    def __init__(self, poll_interval_sec=0.5, timeout_sec=10.0):
        super().__init__(
            name="counter",
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )
        self.check_count = 0

    def _check(self):
        self.check_count += 1
        return f"check_{self.check_count}"


def test_throttle_reduces_check_calls():
    """100ms tick 调用 poll() 50 次，_check() 应只被调用 ~5 次（poll_interval=0.5s）"""
    p = CounterPoller(poll_interval_sec=0.5, timeout_sec=10.0)
    for _ in range(50):  # 50 次 ≈ 模拟 5 秒 100ms tick
        p.poll()
    elapsed = time.monotonic() - p._start_time
    expected = max(1, int(elapsed / 0.5))
    # 允许 ±2 的误差
    assert abs(p.check_count - expected) <= 2, (
        f"check_count={p.check_count}, elapsed={elapsed:.2f}s, expected~{expected}"
    )
    print(f"  ✓ 节流生效: 5秒内 _check() 调用 {p.check_count} 次（预期 ~{expected}）")


def test_timeout_not_throttled():
    """timeout 判定每次 poll() 都检查（即使在节流窗口内）"""
    p = CounterPoller(poll_interval_sec=10.0, timeout_sec=0.3)
    # 短节流窗口 + 短 timeout
    # 快速调用直到 timeout
    deadline_seen = False
    t0 = time.monotonic()
    for _ in range(20):
        result = p.poll()
        if result.is_timeout:
            deadline_seen = True
            assert result.is_complete
            break
        time.sleep(0.05)  # 50ms 一调用
    elapsed = time.monotonic() - t0
    assert deadline_seen, f"应在 timeout 内检测到 is_timeout，但 check_count={p.check_count}"
    # _check 应只调用过 1-2 次（节流生效），但 is_timeout 必须能及时判定
    assert p.check_count <= 3, f"check_count={p.check_count} 应被节流限制"
    print(f"  ✓ timeout 判定未节流: {elapsed*1000:.0f}ms 内检测到 timeout, check_count={p.check_count}")


def test_completion_sticky():
    """_check() 标记 _is_complete 后，即使后续 tick 也不重复 _check"""
    class OneShotPoller(Poller):
        def __init__(self):
            super().__init__(name="one-shot", timeout_sec=10.0, poll_interval_sec=0.0)
            self.check_count = 0
        def _check(self):
            self.check_count += 1
            self._complete()
            return "done"

    p = OneShotPoller()
    results = [p.poll() for _ in range(10)]
    # 第一次 _check 后 _is_complete=True，但后续的 poll() 不应再次 _check
    assert p.check_count == 1, f"_check 应仅调用 1 次，实际 {p.check_count}"
    assert all(r.is_complete for r in results)
    print(f"  ✓ is_complete 后不重复 _check: check_count={p.check_count}")


def test_no_throttle_when_interval_zero():
    """poll_interval_sec=0 时不节流（兼容现有调用方）"""
    class FastPoller(Poller):
        def __init__(self):
            super().__init__(name="fast", timeout_sec=10.0, poll_interval_sec=0.0)
            self.check_count = 0
        def _check(self):
            self.check_count += 1
            return None

    p = FastPoller()
    for _ in range(10):
        p.poll()
    assert p.check_count == 10, f"interval=0 时应每次都 _check，实际 {p.check_count}"
    print(f"  ✓ interval=0 不节流: check_count={p.check_count}")


def test_simulates_100ms_tick_with_5s_poll():
    """最贴近真实场景：tick=100ms, OutcomePoller interval=5s"""
    class FakeOutcomePoller(Poller):
        def __init__(self):
            super().__init__(
                name="fake-outcome",
                timeout_sec=900.0,
                poll_interval_sec=5.0,
            )
            self.check_count = 0
        def _check(self):
            self.check_count += 1
            return "pending"

    p = FakeOutcomePoller()
    # 模拟 30 秒 100ms tick
    t0 = time.monotonic()
    while time.monotonic() - t0 < 0.5:  # 缩短测试到 0.5s
        p.poll()
        time.sleep(0.05)
    elapsed = time.monotonic() - t0
    # 5s 间隔下 0.5s 内只应 _check 1 次（首次启动时）
    assert p.check_count <= 1, (
        f"5s 节流下 0.5s 内 _check 应 ≤1，实际 {p.check_count}"
    )
    print(f"  ✓ 真实场景: 0.5s/100ms-tick + 5s poll_interval → check_count={p.check_count}")


if __name__ == "__main__":
    test_throttle_reduces_check_calls()
    test_timeout_not_throttled()
    test_completion_sticky()
    test_no_throttle_when_interval_zero()
    test_simulates_100ms_tick_with_5s_poll()
    print("ALL POLLER THROTTLE TESTS PASSED")
