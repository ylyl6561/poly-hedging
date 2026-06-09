from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stderr


def is_direct_clob_debug_enabled() -> bool:
    flag = os.environ.get("DIRECT_CLOB_DEBUG", "")
    return flag.strip().lower() in {"1", "true", "yes", "on"}


def should_suppress_known_direct_clob_stderr_line(line: str) -> bool:
    text = (line or "").strip()
    return (
        "[py_clob_client_v2]" in text
        and "request error status=400" in text
        and "/auth/api-key" in text
        and "Could not create api key" in text
    )


def direct_clob_debug(event: str, **fields):
    if not is_direct_clob_debug_enabled():
        return
    safe_fields = {key: fields[key] for key in sorted(fields)}
    # print(f"[direct_clob_debug] {event} {json.dumps(safe_fields, ensure_ascii=False, sort_keys=True)}", file=sys.stderr)


def call_with_optional_stderr_suppression(func, *args, **kwargs):
    stderr_buffer = io.StringIO()
    with redirect_stderr(stderr_buffer):
        result = func(*args, **kwargs)
    suppressed_output = stderr_buffer.getvalue()
    if not suppressed_output:
        return result

    passthrough_lines = []
    suppressed_lines = []
    for raw_line in suppressed_output.splitlines():
        if should_suppress_known_direct_clob_stderr_line(raw_line):
            suppressed_lines.append(raw_line)
        elif raw_line.strip():
            passthrough_lines.append(raw_line)

    if passthrough_lines:
        print("\n".join(passthrough_lines), file=sys.stderr)
    if suppressed_lines and is_direct_clob_debug_enabled():
        direct_clob_debug("suppressed_known_sdk_stderr", lines=suppressed_lines[-5:])
    return result
