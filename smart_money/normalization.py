from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


def as_decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value if value is not None else default))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def as_int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def as_json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def as_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)) or str(value).replace(".", "", 1).isdigit():
        epoch = float(value)
        if epoch > 10_000_000_000:
            epoch /= 1000
        try:
            return datetime.fromtimestamp(epoch, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def stable_fingerprint(*parts: Any) -> str:
    payload = "\x1f".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
