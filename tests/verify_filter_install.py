#!/usr/bin/env python3
"""验证 runtime 启动时 filter 是否正确安装。"""
import sys
import types

# Stub eth_account (生产环境用真实的)
if "eth_account" not in sys.modules:
    _fake = types.ModuleType("eth_account")
    _fake.Account = type("X", (), {})
    sys.modules["eth_account"] = _fake

sys.path.insert(0, "/Users/yuliang/project/poly-hedging")

print("=== Step 1: 替换 sys.stderr 为 mock ===")
original_stderr = sys.stderr

class MockStderr:
    def __init__(self):
        self.lines = []
        self.encoding = "utf-8"
    def write(self, text):
        self.lines.append(text)
        original_stderr.write(f"[mock-stderr] {repr(text)[:80]}")
        original_stderr.flush()
    def flush(self):
        pass

sys.stderr = MockStderr()

print("\n=== Step 2: 导入 api (触发 filter 安装) ===")
import api

print("\n=== Step 3: 检查 filter 是否安装 ===")
import logging
for name in ("py_clob_client_v2", "py_clob_client_v2.http_helpers", "py_clob_client_v2.client"):
    lg = logging.getLogger(name)
    filters = [type(f).__name__ for f in lg.filters]
    print(f"  {name}: filters={filters}")

print("\n=== Step 4: 模拟 SDK logger.error 调用 ===")
sdk_logger = logging.getLogger("py_clob_client_v2.http_helpers")

# 模拟 SDK 内部 httpx 抛 Server disconnected
print("\n--- 场景A: Server disconnected (应该被 filter 吞掉) ---")
sdk_logger.error("[py_clob_client_v2] request error: Server disconnected")
print(f"  mock-stderr 收到 {len(MockStderr.lines)} 条")
print(f"  mock-stderr 内容: {MockStderr.lines}")

print("\n--- 场景B: 业务 400 (应该透传) ---")
sdk_logger.error("[py_clob_client_v2] request error status=400, body={'error':'invalid order'}")
print(f"  mock-stderr 收到 {len(MockStderr.lines)} 条 (应该 +1)")

print("\n=== 结论 ===")
if not any(f for f in MockStderr.lines if "Server disconnected" in str(f)):
    print("✅ filter 有效：Server disconnected 被吞掉")
else:
    print("❌ filter 无效：Server disconnected 仍在 mock-stderr 中")
