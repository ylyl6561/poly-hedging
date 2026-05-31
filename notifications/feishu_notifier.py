"""
Feishu (Lark) trade notification module.

Sends a message to a configured Feishu chat whenever a trade is executed.
Only sends for real (non-simulated) trades — dry-run/paper trades are skipped.

Environment variables required:
    FEISHU_APP_ID
    FEISHU_APP_SECRET
    FEISHU_RECEIVE_ID        (chat_id or open_id)
    FEISHU_RECEIVE_ID_TYPE  (chat_id / open_id, default: chat_id)
"""

from __future__ import annotations

import os
import time
import httpx
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


# ── Config dataclass ──────────────────────────────────────────────────────────

@dataclass
class FeishuConfig:
    app_id: str
    app_secret: str
    receive_id: str
    receive_id_type: str = "chat_id"
    timeout_seconds: int = 10


# ── Tenant token cache ─────────────────────────────────────────────────────────

_TOKEN_EXPIRY_BUFFER_SECONDS = 60

_cached_token: Optional[str] = None
_cached_token_expires_at: float = 0


def _fetch_tenant_access_token(cfg: FeishuConfig) -> str | None:
    """Fetch a new tenant_access_token using app_id + app_secret."""
    try:
        with httpx.Client(timeout=cfg.timeout_seconds) as client:
            resp = client.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={
                    "app_id": cfg.app_id,
                    "app_secret": cfg.app_secret,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                return None
            return data.get("tenant_access_token")
    except Exception:
        return None


def _get_cached_token(cfg: FeishuConfig) -> str | None:
    global _cached_token, _cached_token_expires_at
    now = time.time()
    if _cached_token and now < (_cached_token_expires_at - _TOKEN_EXPIRY_BUFFER_SECONDS):
        return _cached_token
    new_token = _fetch_tenant_access_token(cfg)
    if new_token:
        _cached_token = new_token
        _cached_token_expires_at = now + 7200  # Feishu tokens are valid for 2 hours
    return new_token


# ── Message send ────────────────────────────────────────────────────────────────

def _send_text_message(cfg: FeishuConfig, title: str, body: str) -> bool:
    """
    Send a rich text Feishu message to the configured receive_id.
    Returns True on success, False on failure.
    """
    token = _get_cached_token(cfg)
    if not token:
        return False

    # Truncate body to Feishu's safe limit
    MAX_BODY = 3000
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY] + "\n...(truncated)"

    # Use Feishu interactive card format with a colored header
    payload = {
        "receive_id": cfg.receive_id,
        "msg_type": "interactive",
        "content": _build_card_content(title, body),
    }

    try:
        with httpx.Client(timeout=cfg.timeout_seconds) as client:
            resp = client.post(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=" + cfg.receive_id_type,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("code") == 0
    except Exception:
        return False


def _build_card_content(title: str, body: str) -> str:
    """Build a Feishu interactive card with title + body using text elements."""
    import json
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "blue",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": body,
                },
            }
        ],
    }
    return json.dumps(card, ensure_ascii=False)


# ── Public API ────────────────────────────────────────────────────────────────

def is_configured() -> bool:
    """Return True when all required FEISHU_* env vars are set."""
    return all([
        os.environ.get("FEISHU_APP_ID"),
        os.environ.get("FEISHU_APP_SECRET"),
        os.environ.get("FEISHU_RECEIVE_ID"),
    ])


def build_config() -> Optional[FeishuConfig]:
    """Build a FeishuConfig from environment variables. Returns None if not configured."""
    app_id = os.environ.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET")
    receive_id = os.environ.get("FEISHU_RECEIVE_ID")
    if not all([app_id, app_secret, receive_id]):
        return None
    return FeishuConfig(
        app_id=app_id,
        app_secret=app_secret,
        receive_id=receive_id,
        receive_id_type=os.environ.get("FEISHU_RECEIVE_ID_TYPE") or "chat_id",
    )


# ── Trade notification ────────────────────────────────────────────────────────

def format_trade_message(
    event_name: str,
    side: str,
    shares: float,
    price: float,
    amount: float,
    trade_id: str,
    order_time: datetime,
    is_paper: bool = False,
    # ── New enriched fields ───────────────────────────────────────────────────
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
    """
    Build the Feishu notification title and body for a trade.

    Returns (title, body) strings.
    """
    side_emoji = "✅" if side.upper() == "YES" else "❌"
    paper_tag = " [PAPER]" if is_paper else ""
    title = f"{side_emoji} 成交通知{paper_tag}"

    order_time_str = order_time.strftime("%Y-%m-%d %H:%M:%S")
    potential_payout = shares * 1.0  # YES/NO pays $1 per share
    max_profit = potential_payout - amount  # Best case if outcome matches

    body_lines = [
        f"**事件**: {event_name}",
        f"**方向**: {side.upper()} {'(做多 UP)' if side.upper() == 'YES' else '(做多 DOWN)'}",
        f"**下单时间**: {order_time_str}",
        f"",
        f"**─── 订单信息 ───**",
        f"**下单金额**: ${amount:.2f}",
        f"**成交股数**: {shares:.2f}",
        f"**成交价格**: ${price:.4f}",
        f"**trade_id**: {trade_id or 'N/A'}",
        f"",
        f"**─── 收益分析 ───**",
        f"**最大收益**: ${max_profit:.2f} (结算正确时的利润)",
        f"**最大亏损**: -${amount:.2f} (结算错误全损)",
        f"**盈亏比**: {max_profit / amount:.2f}x" if amount > 0 else "",
        f"**胜率要求**: {amount / potential_payout * 100:.1f}%" if potential_payout > 0 else "",
        f"",
        f"**─── 信号质量 ───**",
        f"**入场价格偏离**: {divergence:.3f}",
        f"**Binance延迟**: {feed_lag_ms}ms",
        f"**方向价差**: ${directional_gap_usd:.2f}",
    ]

    # Add market context if available
    if remaining_sec > 0 or price_to_beat > 0 or binance_price > 0:
        body_lines.extend([
            "",
            f"**─── 市场上下文 ───**",
        ])
        if remaining_sec > 0:
            body_lines.append(f"**距结算**: {remaining_sec:.0f}s")
        if price_to_beat > 0:
            body_lines.append(f"**基准价(Chainlink)**: ${price_to_beat:,.2f}")
        if binance_price > 0:
            body_lines.append(f"**Binance价格**: ${binance_price:,.2f}")
            if price_to_beat > 0:
                binance_vs_beat = binance_price - price_to_beat
                body_lines.append(f"**Binance相对基准**: {'+' if binance_vs_beat >= 0 else ''}{binance_vs_beat:.2f}")
        if chainlink_price > 0 and binance_price > 0:
            gap = binance_price - chainlink_price
            body_lines.append(f"**Binance-Chainlink价差**: {'+' if gap >= 0 else ''}{gap:.2f}")

    body = "\n".join(body_lines)
    return title, body


async def send_trade_notification_async(
    event_name: str,
    side: str,
    shares: float,
    price: float,
    amount: float,
    trade_id: str,
    order_time: datetime,
    is_paper: bool = False,
    # ── Enriched fields ─────────────────────────────────────────────────────────
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
    """
    Send a trade notification to Feishu asynchronously.
    Only sends for real (non-paper) trades.

    Returns True if the message was sent successfully, False otherwise.
    """
    if is_paper:
        return False

    cfg = build_config()
    if cfg is None:
        return False

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

    return _send_text_message(cfg, title, body)


def send_trade_notification_sync(
    event_name: str,
    side: str,
    shares: float,
    price: float,
    amount: float,
    trade_id: str,
    order_time: Optional[datetime] = None,
    is_paper: bool = False,
    # ── Enriched fields ─────────────────────────────────────────────────────────
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
    """
    Send a trade notification to Feishu synchronously.
    Convenience wrapper for use in non-async code.

    Returns True if the message was sent successfully, False otherwise.
    """
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

    return _send_text_message(cfg, title, body)


def _fmt_usd(value) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "N/A"


def _portfolio_total_usdc(portfolio: dict) -> float | None:
    if not isinstance(portfolio, dict):
        return None
    for key in (
        "total_value_usdc",
        "portfolio_value_usdc",
        "account_value_usdc",
        "net_liquidation_usdc",
        "equity_usdc",
    ):
        value = portfolio.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    balance = portfolio.get("balance_usdc")
    positions_value = None
    for key in ("positions_value_usdc", "open_positions_value_usdc", "market_value_usdc"):
        if portfolio.get(key) is not None:
            positions_value = portfolio.get(key)
            break
    if balance is not None and positions_value is not None:
        try:
            return float(balance) + float(positions_value)
        except (TypeError, ValueError):
            pass
    if balance is not None:
        try:
            return float(balance)
        except (TypeError, ValueError):
            pass
    return None


def format_asset_snapshot_message(
    portfolio: dict,
    trigger_event: str = "",
    trade_id: str = "",
    scheduled_time: Optional[datetime] = None,
    settlement_outcome: str = "",
    settlement_source: str = "",
    trade_side: str = "",
    trade_amount=None,
) -> tuple[str, str]:
    """Build a Feishu notification for post-trade account assets."""
    title = "💰 结算后账户资产快照" if settlement_outcome else "💰 账户资产快照"
    total_usdc = _portfolio_total_usdc(portfolio)
    balance_usdc = portfolio.get("balance_usdc") if isinstance(portfolio, dict) else None
    positions_value = None
    for key in ("positions_value_usdc", "open_positions_value_usdc", "market_value_usdc"):
        if isinstance(portfolio, dict) and portfolio.get(key) is not None:
            positions_value = portfolio.get(key)
            break
    scheduled_time = scheduled_time or datetime.now(timezone.utc)

    # ── 资产信息头部模块 ────────────────────────────────────────────────────
    header_lines = []
    if isinstance(portfolio, dict) and not portfolio.get("error"):
        header_lines.append("**─── 账户资产 ───**")
        if total_usdc is not None:
            header_lines.append(f"**资产组合**: {_fmt_usd(total_usdc)}")
        if balance_usdc is not None:
            header_lines.append(f"**现金**: {_fmt_usd(balance_usdc)}")
        if positions_value is not None:
            header_lines.append(f"**持仓市值**: {_fmt_usd(positions_value)}")
        if total_usdc is not None and balance_usdc is not None and positions_value is not None:
            try:
                pos_val = float(positions_value) if positions_value else 0
                pos_pct = (pos_val / total_usdc * 100) if total_usdc > 0 else 0
                header_lines.append(f"**持仓占比**: {pos_pct:.1f}%")
            except (TypeError, ValueError):
                pass
        header_lines.append("")  # 空行分隔头部和详情
    elif isinstance(portfolio, dict) and portfolio.get("error"):
        header_lines.append(f"**─── 账户资产 ───**")
        header_lines.append(f"**⚠️ 资产读取失败**: {str(portfolio.get('error'))[:80]}")
        header_lines.append("")

    body_lines = [
        *header_lines,
        f"**快照时间**: {scheduled_time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"**触发事件**: {trigger_event or 'N/A'}",
        f"**trade_id**: {trade_id or 'N/A'}",
    ]
    if settlement_outcome:
        body_lines.extend([
            f"**结算结果**: {settlement_outcome}",
            f"**结算来源**: {settlement_source or 'N/A'}",
        ])
    if trade_side:
        body_lines.append(f"**下单方向**: {trade_side.upper()}")
        if settlement_outcome:
            side_won = trade_side.upper() == "YES" and settlement_outcome.lower() in ("up", "yes")
            side_won = side_won or (trade_side.upper() == "NO" and settlement_outcome.lower() in ("down", "no"))
            body_lines.append(f"**本单结果**: {'命中 ✅' if side_won else '未命中 ❌'}")
    if trade_amount is not None:
        body_lines.append(f"**下单金额**: {_fmt_usd(trade_amount)}")
    return title, "\n".join(body_lines)


def send_asset_snapshot_notification_sync(
    portfolio: dict,
    trigger_event: str = "",
    trade_id: str = "",
    scheduled_time: Optional[datetime] = None,
    settlement_outcome: str = "",
    settlement_source: str = "",
    trade_side: str = "",
    trade_amount=None,
) -> bool:
    """Send a Feishu account asset snapshot."""
    cfg = build_config()
    if cfg is None:
        return False
    title, body = format_asset_snapshot_message(
        portfolio=portfolio,
        trigger_event=trigger_event,
        trade_id=trade_id,
        scheduled_time=scheduled_time,
        settlement_outcome=settlement_outcome,
        settlement_source=settlement_source,
        trade_side=trade_side,
        trade_amount=trade_amount,
    )
    return _send_text_message(cfg, title, body)
