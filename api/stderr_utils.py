from __future__ import annotations

import io
import json
import logging
import os
import re
import sys
from contextlib import redirect_stderr


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text or "")


def is_direct_clob_debug_enabled() -> bool:
    flag = os.environ.get("DIRECT_CLOB_DEBUG", "")
    return flag.strip().lower() in {"1", "true", "yes", "on"}


def _is_known_transient_noise_message(record: logging.LogRecord) -> bool:
    """判断 logger record 是否是已知的"传输层瞬时噪音"。

    py_clob_client_v2 在 httpx 抛 RequestError 时会 logger.error 一行
    "[py_clob_client_v2] request error: Server disconnected" 等。
    这些是 SDK 内部的瞬时噪音（下一 tick 自动重试），对运维无价值。

    返回 True → 应该被拦截。
    """
    msg = _strip_ansi(record.getMessage() or "").strip()
    if "[py_clob_client_v2]" not in msg:
        return False
    # 业务 / 真实 HTTP 错误（如 status=400 body={"error":...}、status=5xx、status=429）
    # 不视为噪音；只过滤传输层断开类消息。
    transient_markers = (
        "request error: Server disconnected",
        "request error: Connection reset by peer",
        "request error: RemoteProtocolError",
        "request error: ConnectError",
        "request error: ReadError",
        "request error: WriteError",
    )
    return any(marker in msg for marker in transient_markers)


class _TransientHttpNoiseFilter(logging.Filter):
    """logging.Filter：拦截 SDK 已知瞬时传输噪音。

    背景：
    - py_clob_client_v2 在 HTTP 失败时调 logger.error("[py_clob_client_v2] request error: %s", exc)
    - exc 是 httpx 的 RequestError 子类（如 RemoteProtocolError("Server disconnected")）
    - 默认 logging lastResort handler 直接写到 sys.stderr
    - 我们的 `call_with_optional_stderr_suppression` 用 redirect_stderr 包装调用 site，
      但很多 CLOB client 调用 (get_order / cancel_order / get_market / create_and_post_*) 没用 wrapper
    - 即便用了 wrapper，异常路径下 buffer 内容被函数异常退出丢弃

    解决方案：在 SDK 的 logger (py_clob_client_v2.http_helpers) 上挂这个 filter，
    一次性彻底拦截已知噪音。无需修改所有调用 site。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if _is_known_transient_noise_message(record):
            return False  # 阻止输出
        return True


# SDK 内部 logger 的实际名称。
# helpers.py 内部用 logging.getLogger(__name__) → "py_clob_client_v2.http_helpers.helpers"
# client.py 内部用 logging.getLogger(__name__) → "py_clob_client_v2.client"
_SDK_LOGGER_NAMES = (
    "py_clob_client_v2.http_helpers.helpers",
    "py_clob_client_v2.http_helpers",
    "py_clob_client_v2.client",
    "py_clob_client_v2",
)


def install_sdk_logger_noise_filter() -> bool:
    """在 SDK logger 上安装噪音 filter。

    幂等：多次调用只安装一次。返回是否实际执行了安装。
    项目入口（main/fastloop_trader.py、tests、scripts）应在最早阶段调用一次。

    注意：如果之前调用过且 SDK logger 还没被首次创建（懒加载），filter 仍会在
    logger 首次被访问时生效，因为 filter 是按 name 安装到 _SDK_LOGGER_NAMES
    里的 logger 及其子 logger。
    """
    import logging as _logging
    filter_obj = _TransientHttpNoiseFilter()
    installed = False
    for name in _SDK_LOGGER_NAMES:
        logger = _logging.getLogger(name)
        # 避免重复安装
        if any(isinstance(f, _TransientHttpNoiseFilter) for f in logger.filters):
            continue
        logger.addFilter(filter_obj)
        installed = True
    return installed


def should_suppress_known_direct_clob_stderr_line(line: str) -> bool:
    text = _strip_ansi(line).strip()
    if not text.startswith("[py_clob_client_v2]"):
        return False
    if "request error status=400" in text and "/auth/api-key" in text and "Could not create api key" in text:
        return True
    if "request error: Server disconnected" in text:
        return True
    if "request error: Connection reset by peer" in text:
        return True
    if "request error: RemoteProtocolError" in text:
        return True
    return False


def direct_clob_debug(event: str, **fields):
    if not is_direct_clob_debug_enabled():
        return
    safe_fields = {key: fields[key] for key in sorted(fields)}
    # print(f"[direct_clob_debug] {event} {json.dumps(safe_fields, ensure_ascii=False, sort_keys=True)}", file=sys.stderr)


def call_with_optional_stderr_suppression(func, *args, **kwargs):
    stderr_buffer = io.StringIO()
    try:
        with redirect_stderr(stderr_buffer):
            result = func(*args, **kwargs)
    finally:
        # 即使 func 抛异常也读 buffer，否则 SDK 的 logger 输出会被静默丢弃。
        # 这里只处理 redirect_stderr 捕获到的部分（用了 wrapper 的调用 site）；
        # 没用 wrapper 的调用 site 由 logging.Filter（install_sdk_logger_noise_filter）处理。
        suppressed_output = stderr_buffer.getvalue()
        if suppressed_output:
            passthrough_lines = []
            suppressed_lines = []
            for raw_line in suppressed_output.splitlines():
                if should_suppress_known_direct_clob_stderr_line(raw_line):
                    suppressed_lines.append(raw_line)
                elif raw_line.strip():
                    passthrough_lines.append(raw_line)
            if passthrough_lines:
                try:
                    print("\n".join(passthrough_lines), file=sys.stderr)
                except Exception:
                    pass
            if suppressed_lines and is_direct_clob_debug_enabled():
                direct_clob_debug("suppressed_known_sdk_stderr", lines=suppressed_lines[-5:])
    return result
