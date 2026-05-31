from trading.oracle_settlement_no import evaluate_chainlink_settlement_no_signal


BASE_CFG = {
    "oracle_settlement_no_enabled": True,
    "oracle_settlement_no_entry_start_sec": 20,
    "oracle_entry_end_sec": 3,
    "oracle_settlement_no_min_chainlink_margin_usd": 8.0,
    "oracle_settlement_no_binance_reverse_veto_usd": 4.0,
    "oracle_settlement_no_max_entry_price": 0.88,
    "oracle_settlement_no_min_payout_edge": 0.06,
}


def evaluate(**overrides):
    params = {
        "chainlink_margin": -10.0,
        "binance_margin": -1.0,
        "feed_gap": 0.5,
        "feed_lag_ms": 300,
        "remaining": 12.0,
        "entry_price": 0.82,
        "has_executable_ask": True,
        "cfg": BASE_CFG,
        "dry_run": False,
    }
    params.update(overrides)
    return evaluate_chainlink_settlement_no_signal(**params)


def test_accepts_fresh_chainlink_settlement_no():
    result = evaluate()

    assert result["accepted"] is True
    assert result["reason"] == "settlement_based_no_signal"
    assert result["chainlink_down_edge"] == 10.0


def test_requires_chainlink_clearly_below_open():
    result = evaluate(chainlink_margin=-4.0)

    assert result["accepted"] is False
    assert result["reason"] == "chainlink_not_down_enough_for_no"


def test_binance_margin_does_not_veto_settlement_no():
    """Settlement NO strategy is based on Chainlink direction, not Binance momentum."""
    result = evaluate(binance_margin=5.0)

    # Binance margin doesn't affect settlement NO - only Chainlink direction matters
    assert result["accepted"] is True
    assert result["reason"] == "settlement_based_no_signal"


def test_rejects_expensive_no_entry():
    result = evaluate(entry_price=0.91)

    assert result["accepted"] is False
    assert result["reason"] == "settlement_no_entry_too_expensive"


def test_requires_executable_ask_for_live():
    result = evaluate(has_executable_ask=False)

    assert result["accepted"] is False
    assert result["reason"] == "missing_executable_no_ask"


def test_allows_midpoint_fallback_only_for_dry_run():
    result = evaluate(has_executable_ask=False, dry_run=True)

    assert result["accepted"] is True
