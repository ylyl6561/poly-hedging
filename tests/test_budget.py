"""Tests for api.budget — sliding-window request budgets."""

from __future__ import annotations

import time

from api.budget import (
    DEFAULT_DOMAIN_BUDGETS,
    BudgetConfig,
    BudgetRegistry,
    SlidingBudget,
    default_config_for,
)


def test_basic_budget_allows_up_to_limit():
    b = SlidingBudget(
        BudgetConfig(window_seconds=60, window_buckets=6, max_requests_per_window=3)
    )
    assert b.allow() is True
    assert b.allow() is True
    assert b.allow() is True
    assert b.allow() is False


def test_budget_window_elapses():
    b = SlidingBudget(
        BudgetConfig(
            window_seconds=0.2, window_buckets=2, max_requests_per_window=2
        )
    )
    assert b.allow() is True
    assert b.allow() is True
    assert b.allow() is False
    time.sleep(0.35)  # > window_seconds + bucket_width
    assert b.allow() is True


def test_budget_increments_total():
    b = SlidingBudget(
        BudgetConfig(window_seconds=60, window_buckets=6, max_requests_per_window=5)
    )
    for _ in range(5):
        assert b.allow() is True
    assert b.current_usage() == 5
    assert b.allow() is False


def test_invalid_bucket_count():
    import pytest

    with pytest.raises(ValueError):
        BudgetConfig(window_buckets=1)


def test_registry_per_route_isolation():
    reg = BudgetRegistry()
    a1 = reg.for_route("data-api.polymarket.com", "leaderboard")
    a2 = reg.for_route("data-api.polymarket.com", "leaderboard")
    b = reg.for_route("data-api.polymarket.com", "activity")
    assert a1 is a2
    assert a1 is not b  # different route = different bucket


def test_default_domain_budgets_conservative():
    # data-api should be tightest
    assert DEFAULT_DOMAIN_BUDGETS["data-api.polymarket.com"] <= 60
    assert DEFAULT_DOMAIN_BUDGETS["data-api.polymarket.com"] >= 10


def test_default_config_for_returns_consistent_values():
    cfg = default_config_for("data-api.polymarket.com", "leaderboard")
    assert cfg.max_requests_per_window == DEFAULT_DOMAIN_BUDGETS["data-api.polymarket.com"]
    cfg2 = default_config_for("unknown.example", "anything")
    # unknown domain falls back to default 60
    assert cfg2.max_requests_per_window == 60


def test_budget_does_not_consume_when_blocked():
    b = SlidingBudget(
        BudgetConfig(window_seconds=60, window_buckets=6, max_requests_per_window=2)
    )
    b.allow()
    b.allow()
    assert b.allow() is False
    # Should not have consumed the third
    assert b.current_usage() == 2