"""
Feishu/Lark messaging utilities.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Optional

import httpx


@dataclass
class FeishuConfig:
    app_id: str
    app_secret: str
    receive_id: str
    receive_id_type: str = "chat_id"
    timeout_seconds: int = 10


_TOKEN_EXPIRY_BUFFER_SECONDS = 60
_cached_token: Optional[str] = None
_cached_token_expires_at: float = 0


def is_configured() -> bool:
    return all([os.environ.get("FEISHU_APP_ID"), os.environ.get("FEISHU_APP_SECRET"), os.environ.get("FEISHU_RECEIVE_ID")])


def build_config() -> Optional[FeishuConfig]:
    app_id = os.environ.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET")
    receive_id = os.environ.get("FEISHU_RECEIVE_ID")
    if not all([app_id, app_secret, receive_id]):
        return None
    return FeishuConfig(app_id=app_id, app_secret=app_secret, receive_id=receive_id, receive_id_type=os.environ.get("FEISHU_RECEIVE_ID_TYPE") or "chat_id")


def _fetch_tenant_access_token(cfg: FeishuConfig) -> str | None:
    try:
        with httpx.Client(timeout=cfg.timeout_seconds) as client:
            resp = client.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal", json={"app_id": cfg.app_id, "app_secret": cfg.app_secret})
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
    token = _fetch_tenant_access_token(cfg)
    if token:
        _cached_token = token
        _cached_token_expires_at = now + 7200
    return token


def _build_card_content(title: str, body: str) -> str:
    card = {"config": {"wide_screen_mode": True}, "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"}, "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": body}}]}
    return json.dumps(card, ensure_ascii=False)


def send_feishu_message(title: str, body: str, *, cfg: FeishuConfig | None = None) -> bool:
    cfg = cfg or build_config()
    if cfg is None:
        return False
    token = _get_cached_token(cfg)
    if not token:
        return False
    if len(body) > 3000:
        body = body[:3000] + "\n...(truncated)"
    payload = {"receive_id": cfg.receive_id, "msg_type": "interactive", "content": _build_card_content(title, body)}
    try:
        with httpx.Client(timeout=cfg.timeout_seconds) as client:
            resp = client.post("https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=" + cfg.receive_id_type, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, json=payload)
            resp.raise_for_status()
            return resp.json().get("code") == 0
    except Exception:
        return False


def send_feishu_markdown(body: str, *, title: str = "通知", cfg: FeishuConfig | None = None) -> bool:
    return send_feishu_message(title, body, cfg=cfg)


def send_feishu_text(text: str, *, title: str = "通知", cfg: FeishuConfig | None = None) -> bool:
    return send_feishu_message(title, text.replace("\n", "\n\n"), cfg=cfg)


def send_feishu_notice(title: str, content: str, *, level: str = "info", cfg: FeishuConfig | None = None) -> bool:
    level_icon = {"info": "ℹ️", "warn": "⚠️", "error": "❌", "success": "✅"}.get(level, "ℹ️")
    return send_feishu_message(f"{level_icon} {title}", content, cfg=cfg)


def send_feishu(title: str, content: str, *, level: str = "info", cfg: FeishuConfig | None = None) -> bool:
    return send_feishu_notice(title, content, level=level, cfg=cfg)
