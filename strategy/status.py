"""
CLOB order status normalization utilities.

Centralizes the mapping between Polymarket CLOB raw status strings
and our internal OrderStatus enum values.
"""

from strategy.dual_wallet_models import OrderStatus


# CLOB raw status → Normalized status mapping
CLOB_STATUS_MAP: dict[str, str] = {
    # Pending / active orders
    "order_status_live": OrderStatus.SUBMITTED.value,
    "order_status_pending": OrderStatus.SUBMITTED.value,
    "order_status_open": OrderStatus.SUBMITTED.value,
    "live": OrderStatus.SUBMITTED.value,
    "open": OrderStatus.SUBMITTED.value,
    "pending": OrderStatus.SUBMITTED.value,
    # Filled / executed
    "order_status_matched": OrderStatus.FILLED.value,
    "matched": OrderStatus.FILLED.value,
    "filled": OrderStatus.FILLED.value,
    "executed": OrderStatus.FILLED.value,
    # Cancelled
    "order_status_canceled": OrderStatus.CANCELLED.value,
    "cancelled": OrderStatus.CANCELLED.value,
    "canceled": OrderStatus.CANCELLED.value,
    # Failed / rejected
    "failed": OrderStatus.FAILED.value,
    "rejected": OrderStatus.FAILED.value,
}


def normalize_clob_status(status: str) -> str:
    """
    Normalize a CLOB API status string to our internal OrderStatus value.

    Args:
        status: Raw status string from CLOB API (case-insensitive).

    Returns:
        Normalized status string (e.g., "filled", "submitted").
        Falls back to "submitted" if status is empty/None.
    """
    if not status:
        return OrderStatus.SUBMITTED.value
    return CLOB_STATUS_MAP.get(status.lower(), status)


# Convenience sets for common status checks
CLOB_FILLED_STATUSES = {"matched", "filled", "executed"}
CLOB_CANCELLED_STATUSES = {"cancelled", "canceled"}
CLOB_ACTIVE_STATUSES = {"live", "open", "pending"}


def is_clob_filled(status: str) -> bool:
    """Check if a CLOB raw status represents a filled order."""
    return status.lower() in CLOB_FILLED_STATUSES


def is_clob_cancelled(status: str) -> bool:
    """Check if a CLOB raw status represents a cancelled order."""
    return status.lower() in CLOB_CANCELLED_STATUSES
