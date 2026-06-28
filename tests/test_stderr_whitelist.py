"""
回归测试：api.stderr_utils.should_suppress_known_direct_clob_stderr_line 的白名单行为。

白名单（应抑制 = True）：
1. 历史 api-key 400 错误（创建 API key 时的 SDK 内部 noise）
2. request error: Server disconnected（传输层瞬时噪音，下一 tick 会重试）

不应抑制（应透传 = False）：
- 业务错误（status=400 + 非 api-key）
- 真实 HTTP 错误（5xx, 401, 429 等）
- 与 SDK 无关的输出
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# 直接加载 stderr_utils 模块文件，绕过 api/__init__.py 的全套 import 链
# (那条链会拉 accounts → eth_account，本环境缺包)
def _load_stderr_utils():
    path = Path(__file__).resolve().parent.parent / "api" / "stderr_utils.py"
    spec = importlib.util.spec_from_file_location("stderr_utils_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_stderr_utils()
should_suppress_known_direct_clob_stderr_line = _mod.should_suppress_known_direct_clob_stderr_line


# ===== 应抑制：白名单内的行 =====

def test_suppress_historical_api_key_400():
    """历史噪声：create_or_derive_api_key 失败时的 SDK 内部 400。"""
    line = "[py_clob_client_v2] request error status=400, /auth/api-key, Could not create api key"
    assert should_suppress_known_direct_clob_stderr_line(line) is True
    print("  ✓ 抑制历史 api-key 400 错误")


def test_suppress_server_disconnected():
    """新增白名单：传输层瞬时噪音。"""
    line = "[py_clob_client_v2] request error: Server disconnected"
    assert should_suppress_known_direct_clob_stderr_line(line) is True
    print("  ✓ 抑制 Server disconnected 传输层噪音")


def test_suppress_server_disconnected_with_extra_context():
    """SDK 可能在同一条消息中加额外上下文，依然应抑制。"""
    line = "[py_clob_client_v2] request error: Server disconnected during read of /orders/0xa693..."
    assert should_suppress_known_direct_clob_stderr_line(line) is True
    print("  ✓ 抑制带上下文的 Server disconnected")


def test_suppress_server_disconnected_with_ansi_color():
    """SDK 实际日志带 ANSI 颜色码：\\x1b[34m...[\\x1b[0m。子串不连续也能匹配。"""
    line = "\x1b[34m[py_clob_client_v2]\x1b[0m request error: Server disconnected"
    assert should_suppress_known_direct_clob_stderr_line(line) is True
    print("  ✓ 抑制带 ANSI 颜色码的 Server disconnected")


def test_suppress_connection_reset_by_peer():
    """同类传输层噪音。"""
    line = "\x1b[34m[py_clob_client_v2]\x1b[0m request error: Connection reset by peer"
    assert should_suppress_known_direct_clob_stderr_line(line) is True
    print("  ✓ 抑制 Connection reset by peer")


def test_suppress_remote_protocol_error():
    """httpx 抛 RemoteProtocolError 时 SDK 也包成 'request error: ...'。"""
    line = "\x1b[34m[py_clob_client_v2]\x1b[0m request error: RemoteProtocolError"
    assert should_suppress_known_direct_clob_stderr_line(line) is True
    print("  ✓ 抑制 RemoteProtocolError")


# ===== 应透传：业务错误 / 真实 HTTP 错误 =====

def test_pass_through_business_400():
    """业务 400（非 api-key 路径）必须保留。"""
    line = "[py_clob_client_v2] request error status=400, body={\"error\":\"invalid order\"}"
    assert should_suppress_known_direct_clob_stderr_line(line) is False
    print("  ✓ 透传业务 400（非 api-key）")


def test_pass_through_api_key_400_without_marker():
    """400 但缺少 'Could not create api key' 标记：不应抑制（业务错误）。"""
    line = "[py_clob_client_v2] request error status=400, /auth/api-key, different error message"
    assert should_suppress_known_direct_clob_stderr_line(line) is False
    print("  ✓ 透传 api-key 路径的非噪声错误")


def test_pass_through_5xx():
    """5xx 服务端错误：必须保留。"""
    line = "[py_clob_client_v2] request error status=503, service unavailable"
    assert should_suppress_known_direct_clob_stderr_line(line) is False
    print("  ✓ 透传 5xx 错误")


def test_pass_through_429():
    """429 限速：必须保留（业务问题不是传输噪音）。"""
    line = "[py_clob_client_v2] request error status=429, rate limit exceeded"
    assert should_suppress_known_direct_clob_stderr_line(line) is False
    print("  ✓ 透传 429 限速错误")


def test_pass_through_unrelated_sdk_output():
    """与 SDK 无关的输出：保留。"""
    line = "[my_app] order placed successfully"
    assert should_suppress_known_direct_clob_stderr_line(line) is False
    print("  ✓ 透传非 SDK 输出")


def test_pass_through_empty():
    """空行：保留（按设计 passthrough_lines 路径）。"""
    assert should_suppress_known_direct_clob_stderr_line("") is False
    print("  ✓ 透传空行")


def test_pass_through_server_disconnected_without_sdk_tag():
    """Server disconnected 但缺少 SDK tag：不抑制。"""
    line = "[other_module] Server disconnected"
    assert should_suppress_known_direct_clob_stderr_line(line) is False
    print("  ✓ 透传非 SDK 来源的 Server disconnected")


def test_pass_through_business_error_with_ansi():
    """业务 400 带 ANSI 颜色码：必须保留。"""
    line = "\x1b[31m[py_clob_client_v2]\x1b[0m request error status=400, body={\"error\":\"invalid order\"}"
    assert should_suppress_known_direct_clob_stderr_line(line) is False
    print("  ✓ 透传带 ANSI 颜色码的业务 400")


if __name__ == "__main__":
    # 应抑制
    test_suppress_historical_api_key_400()
    test_suppress_server_disconnected()
    test_suppress_server_disconnected_with_extra_context()
    test_suppress_server_disconnected_with_ansi_color()
    test_suppress_connection_reset_by_peer()
    test_suppress_remote_protocol_error()

    # 应透传
    test_pass_through_business_400()
    test_pass_through_api_key_400_without_marker()
    test_pass_through_5xx()
    test_pass_through_429()
    test_pass_through_unrelated_sdk_output()
    test_pass_through_empty()
    test_pass_through_server_disconnected_without_sdk_tag()
    test_pass_through_business_error_with_ansi()

    print("ALL STDERR WHITELIST REGRESSION TESTS PASSED")