"""
Feishu (Lark) trade notification module.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .feishu_tools import (
    FeishuConfig,
    is_configured,
    build_config,
    send_feishu_message,
    send_feishu_markdown,
    send_feishu_text,
)


def format_trade_message(
    event_name: str,
    side: str,
    shares: float,
    price: float,
    amount: float,
    trade_id: str,
    order_time: datetime,
    is_paper: bool = False,
    market_id: str = "",
    remaining_sec: float = 0,
    feed_lag_ms: int = 0,
    directional_gap_usd: float = 0,
    momentum_pct: float = 0,
    divergence: float = 0,
    price_to_beat: float = 0,
    binance_price: float = 0,
    chainlink_price: float = 0,
) -> tuple[str, str]:
    side_emoji = "✅" if side.upper() == "YES" else "❌"
    paper_tag = " [PAPER]" if is_paper else ""
    title = f"{side_emoji} 成交通知{paper_tag}"

    order_time_str = order_time.strftime("%Y-%m-%d %H:%M:%S")
    potential_payout = shares * 1.0
    max_profit = potential_payout - amount

    body_lines = [
        f"**事件**: {event_name}",
        f"**方向**: {side.upper()} {'(做多 UP)' if side.upper() == 'YES' else '(做多 DOWN)'}",
        f"**下单时间**: {order_time_str}",
        "",
        "**─── 订单信息 ───**",
        f"**下单金额**: ${amount:.2f}",
        f"**成交股数**: {shares:.2f}",
        f"**成交价格**: ${price:.4f}",
        f"**trade_id**: {trade_id or 'N/A'}",
        "",
        "**─── 收益分析 ───**",
        f"**最大收益**: ${max_profit:.2f} (结算正确时的利润)",
        f"**最大亏损**: -${amount:.2f} (结算错误全损)",
        f"**盈亏比**: {max_profit / amount:.2f}x" if amount > 0 else "",
        f"**胜率要求**: {amount / potential_payout * 100:.1f}%" if potential_payout > 0 else "",
        "",
        "**─── 信号质量 ───**",
        f"**入场价格偏离**: {divergence:.3f}",
        f"**Binance延迟**: {feed_lag_ms}ms",
        f"**方向价差**: ${directional_gap_usd:.2f}",
    ]

    if remaining_sec > 0 or price_to_beat > 0 or binance_price > 0:
        body_lines.extend(["", "**─── 市场上下文 ───**"])
        if remaining_sec > 0:
            body_lines.append(f"**距结算**: {remaining_sec:.0f}s")
        if price_to_beat > 0:
            body_lines.append(f"**基准价(Chainlink)**: ${price_to_beat:,.2f}")
        if binance_price > 0:
            body_lines.append(f"**Binance价格**: ${binance_price:,.2f}")
            if price_to_beat > 0:
                delta = binance_price - price_to_beat
                body_lines.append(f"**Binance相对基准**: {'+' if delta >= 0 else ''}{delta:.2f}")
        if chainlink_price > 0 and binance_price > 0:
            gap = binance_price - chainlink_price
            body_lines.append(f"**Binance-Chainlink价差**: {'+' if gap >= 0 else ''}{gap:.2f}")

    return title, "\n".join(body_lines)


def send_trade_notification_sync(
    event_name: str,
    side: str,
    shares: float,
    price: float,
    amount: float,
    trade_id: str,
    order_time: Optional[datetime] = None,
    is_paper: bool = False,
    market_id: str = "",
    remaining_sec: float = 0,
    feed_lag_ms: int = 0,
    directional_gap_usd: float = 0,
    momentum_pct: float = 0,
    divergence: float = 0,
    price_to_beat: float = 0,
    binance_price: float = 0,
    chainlink_price: float = 0,
) -> bool:
    if is_paper:
        return False
    cfg = build_config()
    if cfg is None:
        return False
    if order_time is None:
        order_time = datetime.now(timezone.utc)
    title, body = format_trade_message(
        event_name=event_name,
        side=side,
        shares=shares,
        price=price,
        amount=amount,
        trade_id=trade_id,
        order_time=order_time,
        is_paper=is_paper,
        market_id=market_id,
        remaining_sec=remaining_sec,
        feed_lag_ms=feed_lag_ms,
        directional_gap_usd=directional_gap_usd,
        momentum_pct=momentum_pct,
        divergence=divergence,
        price_to_beat=price_to_beat,
        binance_price=binance_price,
        chainlink_price=chainlink_price,
    )
    return send_feishu_message(title, body, cfg=cfg)


def send_trade_notification_async(*args, **kwargs) -> bool:
    return send_trade_notification_sync(*args, **kwargs)


def send_message(title: str, body: str) -> bool:
    return send_feishu_message(title, body)


def send_markdown(body: str, title: str = "通知") -> bool:
    return send_feishu_markdown(body, title=title)


def send_text(text: str, title: str = "通知") -> bool:
    return send_feishu_text(text, title=title)
