"""Feishu (Lark) webhook notifier for smart-money signals.

Sends a one-shot card to the configured webhook when a new auto-executable
signal appears. The card carries two action buttons:

* ``approve_{signal_id}`` — user clicks to confirm and trigger the executor
* ``cancel_{signal_id}``  — user clicks to abort

If no action is taken within ``follow_confirm_timeout_seconds`` the signal
defaults to ``cancel`` (auto-cancel) for safety.

Webhook payload shape (interactive card) is documented at:
https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/create
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from .config import SmartMoneySettings

logger = logging.getLogger(__name__)


def _fmt_signal_card(signal: dict[str, Any], *, dashboard_base: str) -> dict[str, Any]:
    direction_emoji = "🟢" if signal["direction"] == "YES" else "🔴"
    wallets = signal.get("trigger_wallets") or []
    top_wallets = wallets[:5]
    wallet_lines = "\n".join(
        f"• `{w.get('wallet','')[:10]}...`  score={w.get('smart_money_score', 0):.1f}  "
        f"${float(w.get('amount', 0) or 0):.0f}  ({w.get('username') or 'anon'})"
        for w in top_wallets
    )
    risk_reasons = signal.get("risk_reasons") or []
    reasons_text = "\n".join(f"  - {r}" for r in risk_reasons[:5]) or "  (无)"
    title = signal.get("title") or signal["condition_id"]
    cond_id = signal["condition_id"]
    sig_id = signal["id"]
    conf = signal.get("confidence", 0)
    size = signal.get("suggested_size_usdc", 0)

    body_text = (
        f"**{direction_emoji} {signal['direction']} on \"{title[:80]}\"**\n\n"
        f"信号类型：`{signal['signal_type']}`  共识高手数：**{signal.get('trader_count', 0)}**\n"
        f"置信度：**{conf * 100:.0f}%**  建议仓位：**${size:.0f}** USDC\n"
        f"condition_id: `{cond_id}`\n\n"
        f"**触发高手**\n{wallet_lines or '  (无)'}\n\n"
        f"**风控原因**\n{reasons_text}\n\n"
        f"30 秒内未点击「跟单」即自动取消。"
    )
    actions = [
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": f"✅ 跟单 ${size:.0f}"},
            "type": "primary",
            "value": {"action": "approve", "signal_id": sig_id},
        },
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "❌ 取消"},
            "type": "danger",
            "value": {"action": "cancel", "signal_id": sig_id},
        },
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🔗 打开仪表盘"},
            "type": "default",
            "url": f"{dashboard_base}/",
        },
    ]
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"💰 Smart Money {signal['direction']} signal · {signal['signal_type']}",
                },
                "template": "green" if signal["direction"] == "YES" else "red",
            },
            "elements": [
                {"tag": "markdown", "content": body_text},
                {"tag": "hr"},
                {"tag": "action", "actions": actions},
            ],
        },
    }


def send_signal_card(
    webhook_url: str,
    signal: dict[str, Any],
    *,
    dashboard_base: str = "http://localhost:8088",
    timeout: float = 5.0,
) -> tuple[bool, str]:
    """POST the card to Feishu. Returns (success, response_text)."""
    if not webhook_url:
        return False, "feishu_webhook_url not configured"
    payload = _fmt_signal_card(signal, dashboard_base=dashboard_base)
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8")
            return resp.status == 200, data
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.read().decode('utf-8', 'ignore')}"
    except urllib.error.URLError as exc:
        return False, f"URL error: {exc.reason}"
    except Exception as exc:
        return False, f"unexpected: {exc!r}"


def send_dry_run_alert(
    webhook_url: str,
    signal: dict[str, Any],
    *,
    dashboard_base: str = "http://localhost:8088",
) -> tuple[bool, str]:
    """Same card but with a clear dry-run banner."""
    direction_emoji = "🟢" if signal["direction"] == "YES" else "🔴"
    sig = dict(signal)
    sig["title"] = "(DRY-RUN) " + (sig.get("title") or sig["condition_id"])
    sig["risk_reasons"] = ["DRY-RUN: no real order will be placed"] + list(
        sig.get("risk_reasons") or []
    )
    return send_signal_card(webhook_url, sig, dashboard_base=dashboard_base)