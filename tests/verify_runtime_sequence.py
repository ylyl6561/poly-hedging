#!/usr/bin/env python3
"""模拟 fastloop_trader.py 的完整初始化序列，验证 filter + TimestampLineTee 协作。"""
import sys
import types
from pathlib import Path

# Stub eth_account
if "eth_account" not in sys.modules:
    _fake = types.ModuleType("eth_account")
    _fake.Account = type("X", (), {})
    sys.modules["eth_account"] = _fake

PROJECT_ROOT = Path("/Users/yuliang/project/poly-hedging")
sys.path.insert(0, str(PROJECT_ROOT))

print("=== 1. 模拟 runtime: 导入 api (触发 filter 安装) ===")
# 在 TimestampLineTee 之前导入，模拟真实顺序
import api

print("\n=== 2. 检查 filter 是否安装 ===")
import logging
for name in ("py_clob_client_v2", "py_clob_client_v2.http_helpers"):
    lg = logging.getLogger(name)
    print(f"  {name}: effectiveLevel={lg.getEffectiveLevel()}, filters={[type(f).__name__ for f in lg.filters]}")

print("\n=== 3. 模拟 setup_run_logging: 用 StringIO 替代文件 ===")
from io import StringIO
mock_file = StringIO()
mock_structured_log = None

class TimestampLineTee:
    def __init__(self, original_stream, file_obj, structured_log=None):
        self.original_stream = original_stream
        self.file_obj = file_obj
        self._buf = ""

    def write(self, text):
        if not text:
            return 0
        self.original_stream.write(text)
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            from datetime import datetime
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            self.file_obj.write(f"[{ts}] {line}\n")
        return len(text)

    def flush(self):
        self.original_stream.flush()

original_stderr = sys.stderr
sys.stderr = TimestampLineTee(original_stderr, mock_file)

print("\n=== 4. 模拟 SDK logger.error (传输层噪音) ===")
sdk_logger = logging.getLogger("py_clob_client_v2.http_helpers")
sdk_logger.error("[py_clob_client_v2] request error: Server disconnected")

print(f"\n=== 5. 检查结果 ===")
log_content = mock_file.getvalue()
if log_content:
    print(f"  ❌ 消息出现在日志中 ({len(log_content)} chars):")
    for line in log_content.strip().split("\n"):
        print(f"     {line}")
else:
    print("  ✅ 消息被正确拦截，未出现在日志中")

# 检查 stderr（原始流）
print(f"\n=== 6. 检查原始 stderr ===")
# 不容易直接读 original_stderr 内容，但可以验证

print("\n=== 7. 模拟业务错误 (应透传) ===")
sdk_logger.error("[py_clob_client_v2] request error status=400, body={'error':'invalid order'}")
log_content2 = mock_file.getvalue()
if "invalid order" in log_content2:
    print("  ✅ 业务错误正确透传")
else:
    print("  ❌ 业务错误被误杀")
