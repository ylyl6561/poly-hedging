"""基于 Chainlink 结算的 NO 策略信号门控。

与 YES 延迟策略不同，NO 策略专注于 Chainlink 已相对开盘价下跌的情况：
- Settlement NO: Chainlink 已低于开盘价 - 阈值 → 买 NO（赌结算价格会拉低）

核心假设：Chainlink 价格最终会成为 Polymarket 的结算依据。
当 Chainlink 相对开盘价下跌超过阈值时，购买 NO 有概率获利。
"""


def evaluate_chainlink_settlement_no_signal(
    *,
    chainlink_margin,
    binance_margin,
    feed_gap,
    feed_lag_ms,
    remaining,
    entry_price,
    has_executable_ask,
    cfg,
    dry_run,
):
    """评估基于延迟的对称 NO 策略信号。

    与 YES 策略对称的条件：
    1. 时间窗口 (3s~30s)
    2. Chainlink 不矛盾 (chainlink_margin <= max_contrary_usd)
    3. Chainlink 方向性差距 (chainlink_down_edge >= min_usd)
    4. Binance 延迟验证 (feed_lag_ms >= min_ms AND directional_gap >= min_usd)
    5. 流动性检查
    6. 入场价格检查
    7. payout_edge 检查
    """
    if not cfg.get("oracle_settlement_no_enabled", True):
        return {"accepted": False, "reason": "settlement_no_disabled"}

    # 条件 1: 时间窗口 (与 YES 共用)
    start_sec = cfg.get("oracle_entry_start_sec", 30)
    end_sec = cfg.get("oracle_entry_end_sec", 3)
    if remaining > start_sec:
        return {"accepted": False, "reason": "settlement_no_too_early"}
    if remaining < end_sec:
        return {"accepted": False, "reason": "settlement_no_too_late"}

    # 条件 2: Chainlink 不矛盾
    # 对于 NO 方向，Chainlink 应该 <= price_to_beat + 阈值
    # 即 chainlink_margin <= oracle_max_chainlink_contrary_usd
    max_contrary = cfg.get("oracle_max_chainlink_contrary_usd", 3.0)
    if chainlink_margin > max_contrary:
        return {
            "accepted": False,
            "reason": "chainlink_contradicts_no_direction",
            "chainlink_margin": chainlink_margin,
            "threshold": max_contrary,
        }

    # 条件 3: Chainlink 方向性差距 (NO: Chainlink 必须下跌 >= 阈值)
    # Chainlink 下跌程度 = -chainlink_margin
    min_chainlink_margin = cfg.get("oracle_settlement_no_min_chainlink_margin_usd", 10.0)
    chainlink_down_edge = -float(chainlink_margin)
    if chainlink_down_edge < min_chainlink_margin:
        return {
            "accepted": False,
            "reason": "chainlink_not_down_enough_for_no",
            "chainlink_down_edge": chainlink_down_edge,
            "required": min_chainlink_margin,
        }

    # 条件 4: 基础延迟验证 (Settlement NO 不需要 directional_gap)
    # Settlement NO 的核心信号是 Chainlink 相对开盘价已下跌，而非 Binance-Chainlink 价差方向
    # 只需要确保两个数据源有足够延迟即可（延迟意味着 Chainlink 可能还未完全反映最新价格）
    min_feed_lag_ms = cfg.get("oracle_min_feed_lag_ms", 250)
    if feed_lag_ms < min_feed_lag_ms:
        return {
            "accepted": False,
            "reason": "insufficient_feed_lag_for_settlement_no",
            "feed_lag_ms": feed_lag_ms,
            "required_lag_ms": min_feed_lag_ms,
        }

    # 条件 5: 流动性检查
    if not has_executable_ask and not dry_run:
        return {"accepted": False, "reason": "missing_executable_no_ask"}
    if entry_price is None:
        return {"accepted": False, "reason": "missing_no_entry_price"}

    # 条件 6: 入场价格检查（NO 阈值与主单保持一致）
    entry_price = float(entry_price)
    max_entry = cfg.get("oracle_settlement_no_max_entry_price", 0.92)
    if entry_price > max_entry:
        return {
            "accepted": False,
            "reason": "settlement_no_entry_too_expensive",
            "entry_price": entry_price,
            "max_entry": max_entry,
        }

    # 条件 7: payout_edge 检查（NO 阈值与主单保持一致）
    payout_edge = 1.0 - entry_price
    min_payout_edge = cfg.get("oracle_settlement_no_min_payout_edge", 0.08)
    if payout_edge < min_payout_edge:
        return {
            "accepted": False,
            "reason": "settlement_no_payout_edge_too_small",
            "payout_edge": payout_edge,
            "required": min_payout_edge,
        }

    return {
        "accepted": True,
        "reason": "settlement_based_no_signal",
        "chainlink_down_edge": chainlink_down_edge,
        "feed_lag_ms": feed_lag_ms,
        "payout_edge": payout_edge,
    }
