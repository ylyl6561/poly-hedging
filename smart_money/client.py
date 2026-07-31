"""Backward-compatible shim for the legacy ``PolymarketReadClient`` name.

The implementation now lives in :mod:`api.polymarket_public` so it can be
reused by any module that needs browser-shaped access to the public
Polymarket Data and Gamma APIs (collector, dashboard, ad-hoc scripts,
tests).  This file is kept so existing imports of
``smart_money.client.PolymarketReadClient`` keep working.
"""

from __future__ import annotations

from api.polymarket_public import (
    BROWSER_HEADERS,
    PolymarketPublicClient,
    PublicClientSettings,
    TTLCache,
)

# Legacy alias — keep the old name so existing call sites and tests
# continue to work without modification.
PolymarketReadClient = PolymarketPublicClient

__all__ = [
    "BROWSER_HEADERS",
    "PolymarketPublicClient",
    "PolymarketReadClient",
    "PublicClientSettings",
    "TTLCache",
]