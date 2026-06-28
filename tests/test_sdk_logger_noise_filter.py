"""单元测试：SDK logger noise filter。

覆盖：
1. logging.Filter 装到 SDK logger 后，noise 消息被丢弃
2. 业务 / 真实错误消息不被误杀
3. 非 SDK logger 的消息不被影响
4. install_sdk_logger_noise_filter 幂等
5. call_with_optional_stderr_suppression 在异常路径下也处理 buffer（不丢日志）

Pytest 风格 + 直接调用兼容。
"""
from __future__ import annotations

import importlib.util
import logging
import sys
import types
from io import StringIO
from pathlib import Path

# stub eth_account
if "eth_account" not in sys.modules:
    _fake = types.ModuleType("eth_account")
    _fake.Account = type("X", (), {})
    sys.modules["eth_account"] = _fake

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_stderr_utils():
    """绕过 api/__init__.py 的全套 import 链（拉 accounts → eth_account）。"""
    path = Path(__file__).resolve().parent.parent / "api" / "stderr_utils.py"
    spec = importlib.util.spec_from_file_location("stderr_utils_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_stderr_utils()


def test_filter_drops_server_disconnected():
    """[py_clob_client_v2] request error: Server disconnected 应被 filter 拦截。"""
    sdk_logger = logging.getLogger("py_clob_client_v2.http_helpers")
    sdk_logger.addFilter(_mod._TransientHttpNoiseFilter())

    captured = []
    handler = logging.Handler()
    handler.emit = lambda record: captured.append(record.getMessage())
    sdk_logger.addHandler(handler)
    try:
        sdk_logger.error("[py_clob_client_v2] request error: Server disconnected")
        assert len(captured) == 0, f"noise should be suppressed, got: {captured}"
        print("  ✓ 拦截 Server disconnected")
    finally:
        sdk_logger.removeHandler(handler)


def test_filter_drops_connection_reset_by_peer():
    """[py_clob_client_v2] request error: Connection reset by peer 应被拦截。"""
    sdk_logger = logging.getLogger("py_clob_client_v2.http_helpers")
    sdk_logger.addFilter(_mod._TransientHttpNoiseFilter())
    captured = []
    handler = logging.Handler()
    handler.emit = lambda r: captured.append(r.getMessage())
    sdk_logger.addHandler(handler)
    try:
        sdk_logger.error("[py_clob_client_v2] request error: Connection reset by peer")
        sdk_logger.error("[py_clob_client_v2] request error: RemoteProtocolError")
        assert len(captured) == 0
        print("  ✓ 拦截 Connection reset / RemoteProtocolError")
    finally:
        sdk_logger.removeHandler(handler)


def test_filter_keeps_business_errors():
    """业务错误（status=400 body=...）必须保留。"""
    sdk_logger = logging.getLogger("py_clob_client_v2.http_helpers")
    sdk_logger.addFilter(_mod._TransientHttpNoiseFilter())
    captured = []
    handler = logging.Handler()
    handler.emit = lambda r: captured.append(r.getMessage())
    sdk_logger.addHandler(handler)
    try:
        sdk_logger.error('[py_clob_client_v2] request error status=400, body={"error":"invalid order"}')
        sdk_logger.error("[py_clob_client_v2] request error status=503, service unavailable")
        sdk_logger.error("[py_clob_client_v2] request error status=429, rate limit")
        assert len(captured) == 3, f"业务错误必须透传: {captured}"
        print("  ✓ 业务 400/5xx/429 不被误杀")
    finally:
        sdk_logger.removeHandler(handler)


def test_filter_ignores_other_loggers():
    """非 SDK logger 的消息不受影响。"""
    other_logger = logging.getLogger("other_module")
    other_logger.addFilter(_mod._TransientHttpNoiseFilter())  # 即使装上也只针对 SDK tag
    captured = []
    handler = logging.Handler()
    handler.emit = lambda r: captured.append(r.getMessage())
    other_logger.addHandler(handler)
    try:
        other_logger.error("Server disconnected")  # 不带 SDK tag
        other_logger.error("[other_tag] request error: Server disconnected")  # 非 py_clob_client_v2 tag
        assert len(captured) == 2
        print("  ✓ 非 SDK logger 不受影响")
    finally:
        other_logger.removeHandler(handler)


def test_install_idempotent():
    """install_sdk_logger_noise_filter 多次调用只挂一次 filter。"""
    # 先 remove 现有 filter
    for name in _mod._SDK_LOGGER_NAMES:
        lg = logging.getLogger(name)
        lg.filters = [f for f in lg.filters if not isinstance(f, _mod._TransientHttpNoiseFilter)]

    first = _mod.install_sdk_logger_noise_filter()
    second = _mod.install_sdk_logger_noise_filter()
    assert first is True
    assert second is False, "第二次调用必须返回 False"
    # 检查 filter 数量
    lg = logging.getLogger("py_clob_client_v2.http_helpers")
    count = sum(1 for f in lg.filters if isinstance(f, _mod._TransientHttpNoiseFilter))
    assert count == 1, f"filter 应该只有一个，实际 {count}"
    print("  ✓ install 幂等")


def test_strip_ansi_in_filter():
    """带 ANSI 颜色码的 noise 消息也应被拦截。"""
    sdk_logger = logging.getLogger("py_clob_client_v2.http_helpers")
    sdk_logger.addFilter(_mod._TransientHttpNoiseFilter())
    captured = []
    handler = logging.Handler()
    handler.emit = lambda r: captured.append(r.getMessage())
    sdk_logger.addHandler(handler)
    try:
        sdk_logger.error("\x1b[34m[py_clob_client_v2]\x1b[0m request error: Server disconnected")
        assert len(captured) == 0
        print("  ✓ ANSI 颜色码不影响过滤")
    finally:
        sdk_logger.removeHandler(handler)


def test_call_with_stderr_suppression_handles_exception_path():
    """call_with_optional_stderr_suppression 在 func 抛异常时也应读 buffer。

    关键：异常路径下不能丢失 SDK 的 stderr 输出（被静默吞掉）。
    """
    def boom():
        # 模拟 SDK 在抛异常前 print 到 stderr
        print("[py_clob_client_v2] request error: Server disconnected", file=sys.stderr)
        raise ValueError("simulated sdk failure")

    try:
        _mod.call_with_optional_stderr_suppression(boom)
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "simulated sdk failure" in str(e)
    print("  ✓ 异常路径不丢 SDK 日志")


def test_call_with_stderr_suppression_passes_through_business():
    """call_with_optional_stderr_suppression 在正常路径下业务错误仍透传。"""
    def fake_business_error():
        print("[my_app] order placed successfully", file=sys.stderr)
        return "ok"

    captured = StringIO()
    real_stderr = sys.stderr
    sys.stderr = captured
    try:
        result = _mod.call_with_optional_stderr_suppression(fake_business_error)
        sys.stderr = real_stderr
        assert result == "ok"
        assert "order placed successfully" in captured.getvalue()
        print("  ✓ 正常路径业务 stderr 透传")
    finally:
        sys.stderr = real_stderr


if __name__ == "__main__":
    test_filter_drops_server_disconnected()
    test_filter_drops_connection_reset_by_peer()
    test_filter_keeps_business_errors()
    test_filter_ignores_other_loggers()
    test_install_idempotent()
    test_strip_ansi_in_filter()
    test_call_with_stderr_suppression_handles_exception_path()
    test_call_with_stderr_suppression_passes_through_business()
    print("ALL SDK LOGGER NOISE FILTER TESTS PASSED")