"""Tests for api.circuit_breaker."""

from __future__ import annotations

import time

from api.circuit_breaker import (
    CBState,
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerRegistry,
    DEFAULT_FAILURE_STATUSES,
)


def test_initial_state_is_closed():
    cb = CircuitBreaker("test")
    assert cb.state == CBState.CLOSED
    assert cb.allow() is True


def test_threshold_opens_breaker():
    cb = CircuitBreaker(
        "test", CircuitBreakerConfig(failure_threshold=3, open_duration_seconds=10)
    )
    cb.allow()
    for _ in range(2):
        cb.record_failure(status_code=403)
    assert cb.state == CBState.CLOSED  # not yet at threshold
    cb.record_failure(status_code=429)
    assert cb.state == CBState.OPEN
    assert cb.allow() is False


def test_404_does_not_count():
    """Resource-not-found is a *successful* outcome for the WAF."""
    cb = CircuitBreaker(
        "test", CircuitBreakerConfig(failure_threshold=2)
    )
    for _ in range(5):
        cb.record_failure(status_code=404)
    assert cb.state == CBState.CLOSED
    assert cb.allow() is True


def test_open_then_half_open_then_closed():
    cb = CircuitBreaker(
        "test", CircuitBreakerConfig(failure_threshold=1, open_duration_seconds=0.1)
    )
    cb.allow()
    cb.record_failure(status_code=403)
    assert cb.state == CBState.OPEN
    # Cool-down elapses
    time.sleep(0.15)
    assert cb.state == CBState.HALF_OPEN
    # Probe — only one allowed at a time
    assert cb.allow() is True
    assert cb.allow() is False  # second probe blocked
    # Probe succeeds
    cb.record_success()
    assert cb.state == CBState.CLOSED
    assert cb.allow() is True


def test_half_open_probe_failure_reopens():
    cb = CircuitBreaker(
        "test", CircuitBreakerConfig(failure_threshold=1, open_duration_seconds=0.1)
    )
    cb.allow()
    cb.record_failure(status_code=429)
    assert cb.state == CBState.OPEN
    time.sleep(0.15)
    assert cb.state == CBState.HALF_OPEN
    cb.allow()
    cb.record_failure(status_code=503)
    assert cb.state == CBState.OPEN


def test_success_in_closed_resets_counter():
    cb = CircuitBreaker(
        "test", CircuitBreakerConfig(failure_threshold=3)
    )
    cb.record_failure(status_code=403)
    cb.record_failure(status_code=429)
    cb.record_success()  # reset
    cb.record_failure(status_code=403)
    cb.record_failure(status_code=429)
    # Still 2 failures — would need 3 to open
    assert cb.state == CBState.CLOSED


def test_short_circuit_count_increments():
    cb = CircuitBreaker(
        "test", CircuitBreakerConfig(failure_threshold=1, open_duration_seconds=10)
    )
    cb.allow()
    cb.record_failure(status_code=403)
    for _ in range(5):
        cb.allow()  # short-circuited
    stats = cb.stats()
    assert stats["state"] == CBState.OPEN.value
    assert stats["total_short_circuits"] == 5


def test_failure_statuses_configurable():
    cb = CircuitBreaker(
        "test",
        CircuitBreakerConfig(failure_threshold=1, failure_statuses=frozenset({503})),
    )
    cb.record_failure(status_code=403)  # not in failure_statuses
    assert cb.state == CBState.CLOSED
    cb.record_failure(status_code=503)
    assert cb.state == CBState.OPEN


def test_default_failure_statuses_includes_cf_codes():
    assert 403 in DEFAULT_FAILURE_STATUSES
    assert 429 in DEFAULT_FAILURE_STATUSES
    assert 503 in DEFAULT_FAILURE_STATUSES
    # But not 4xx resource codes
    assert 404 not in DEFAULT_FAILURE_STATUSES
    assert 400 not in DEFAULT_FAILURE_STATUSES


def test_registry_creates_breaker_per_domain():
    reg = CircuitBreakerRegistry()
    a = reg.for_domain("data-api.polymarket.com")
    b = reg.for_domain("data-api.polymarket.com")
    c = reg.for_domain("gamma-api.polymarket.com")
    assert a is b  # same instance
    assert a is not c  # different domain = different breaker
    assert len(reg.stats()) == 2


def test_registry_with_custom_config_for():
    def config_for(domain: str) -> CircuitBreakerConfig:
        return CircuitBreakerConfig(
            failure_threshold=1 if domain == "strict.example" else 10
        )

    reg = CircuitBreakerRegistry(config_for=config_for)
    strict = reg.for_domain("strict.example")
    lenient = reg.for_domain("lenient.example")
    strict.record_failure(status_code=403)
    assert strict.state == CBState.OPEN
    # lenient needs 10 failures
    for _ in range(9):
        lenient.record_failure(status_code=403)
    assert lenient.state == CBState.CLOSED


def test_transitions_audit_log_capped():
    cb = CircuitBreaker(
        "test",
        CircuitBreakerConfig(failure_threshold=1, open_duration_seconds=0.01),
    )
    for _ in range(250):
        cb.allow()
        cb.record_failure(status_code=403)
        # cool-down
        time.sleep(0.012)
        cb.record_success()
    assert len(cb.transitions) <= 200