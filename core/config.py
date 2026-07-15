"""
Configuration management for the fastloop trading system.

This module defines the runtime config schema and resolves module-level values
at import time for the active dual-wallet event flow.
"""

import json
import os
import sys
from pathlib import Path


def load_env_file(skill_file):
    """Load KEY=VALUE pairs from a local .env file without overriding real env vars."""
    start_dir = Path(skill_file).resolve().parent
    candidates = []
    cur = start_dir
    for _ in range(5):
        candidates.append(cur / ".env")
        if cur.parent == cur:
            break
        cur = cur.parent
    env_path = next((p for p in candidates if p.exists()), None)
    if env_path is None:
        return
    try:
        lines = env_path.read_text().splitlines()
    except OSError as exc:
        print(f"Warning: could not read {env_path}: {exc}", file=sys.stderr)
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        os.environ[key] = value


load_env_file(__file__)

CONFIG_SCHEMA = {
    "strategy_mode": {"default": "dual_wallet_event", "env": "SIMMER_FASTLOOP_STRATEGY_MODE", "type": str, "help": "策略模式：dual_wallet_event"},
    "dual_wallet_entry_timeout_sec": {"default": 120, "env": "SIMMER_FASTLOOP_DUAL_WALLET_ENTRY_TIMEOUT_SEC", "type": int, "help": "单边成交等待超时时间（秒）"},
    "dual_wallet_force_close_window_sec": {"default": 40, "env": "SIMMER_FASTLOOP_DUAL_WALLET_FORCE_CLOSE_WINDOW_SEC", "type": int, "help": "距离事件结束多少秒时进入强平窗口"},
    "dual_wallet_fixed_sell_price": {"default": 0.6, "env": "SIMMER_FASTLOOP_DUAL_WALLET_FIXED_SELL_PRICE", "type": float, "help": "首版固定卖出/平仓价格"},
    "dual_wallet_fak_close_price": {"default": 0.5, "env": "SIMMER_FASTLOOP_DUAL_WALLET_FAK_CLOSE_PRICE", "type": float, "help": "双边同时成交后 FAK 强平价格（0-1，建议 0.99）"},
    "dual_wallet_fok_sell_price": {"default": 0.01, "env": "SIMMER_FASTLOOP_DUAL_WALLET_FOK_SELL_PRICE", "type": float, "help": "FOK 纯市价单抛售价格：SELL 表示愿意以 >= 此价成交（撮合价≥此价即可成）。在 FOK_RETRYING 状态下作为动态递减的起点。"},
    "dual_wallet_fok_sell_price_base": {"default": None, "env": "SIMMER_FASTLOOP_DUAL_WALLET_FOK_SELL_PRICE_BASE", "type": float, "help": "FOK 抛售价动态递减的兜底基准（仅在 FOK_RETRYING 状态生效）。为 None 时退化为静态价模式（始终使用 fok_sell_price，不递减）。允许配置为 0 表示愿意白送平仓。"},
    "dual_wallet_fok_sell_price_decay_window_sec": {"default": None, "env": "SIMMER_FASTLOOP_DUAL_WALLET_FOK_SELL_PRICE_DECAY_WINDOW_SEC", "type": float, "help": "FOK 抛售价从初始值匀速递减到 base 的窗口时长（秒）。为 None 时回退为 force_close_window_sec（FOK_RETRYING 状态的最大停留时间）。仅当 base 已配置时才生效。"},
    "dual_wallet_fok_interval_sec": {"default": 1, "env": "SIMMER_FASTLOOP_DUAL_WALLET_FOK_INTERVAL_SEC", "type": float, "help": "FOK 抛售单失败后的重试间隔（秒）"},
    "dual_wallet_entry_up_price": {"default": 0.5, "env": "SIMMER_FASTLOOP_DUAL_WALLET_ENTRY_UP_PRICE", "type": float, "help": "双钱包事件交易的 UP 初始挂单价格"},
    "dual_wallet_entry_down_price": {"default": 0.5, "env": "SIMMER_FASTLOOP_DUAL_WALLET_ENTRY_DOWN_PRICE", "type": float, "help": "双钱包事件交易的 DOWN 初始挂单价格"},
    "dual_wallet_entry_shares": {"default": 10.0, "env": "SIMMER_FASTLOOP_DUAL_WALLET_ENTRY_AMOUNT", "type": float, "help": "每钱包的下单 token 数量（每单固定数量，配合价格算出金额）"},
    "dual_wallet_max_consecutive_losses": {"default": 2, "env": "SIMMER_FASTLOOP_DUAL_WALLET_MAX_CONSECUTIVE_LOSSES", "type": int, "help": "连续亏损达到该次数后停止交易"},
    "dual_wallet_poll_interval_sec": {"default": 0.1, "env": "SIMMER_FASTLOOP_DUAL_WALLET_POLL_INTERVAL_SEC", "type": float, "help": "事件轮询间隔（秒）"},
    "dual_wallet_outcome_poll_interval_sec": {"default": 5, "env": "SIMMER_FASTLOOP_DUAL_WALLET_OUTCOME_POLL_INTERVAL_SEC", "type": int, "help": "事件结束后轮询最终结果的间隔（秒）"},
    "dual_wallet_outcome_poll_timeout_sec": {"default": 600, "env": "SIMMER_FASTLOOP_DUAL_WALLET_OUTCOME_POLL_TIMEOUT_SEC", "type": int, "help": "等待市场最终结果的最长时间（秒）"},
    "dual_wallet_settlement_poll_interval_sec": {"default": 20, "env": "SIMMER_FASTLOOP_DUAL_WALLET_SETTLEMENT_POLL_INTERVAL_SEC", "type": int, "help": "等待结算时轮询钱包余额的间隔（秒）"},
    "dual_wallet_settlement_poll_timeout_sec": {"default": 180, "env": "SIMMER_FASTLOOP_DUAL_WALLET_SETTLEMENT_POLL_TIMEOUT_SEC", "type": int, "help": "等待钱包余额稳定的最长时间（秒）"},
    "dual_wallet_settlement_stable_rounds": {"default": 3, "env": "SIMMER_FASTLOOP_DUAL_WALLET_SETTLEMENT_STABLE_ROUNDS", "type": int, "help": "认定结算完成前，余额连续不变所需轮数"},
    "dual_wallet_event_query_limit": {"default": 20, "env": "SIMMER_FASTLOOP_DUAL_WALLET_EVENT_QUERY_LIMIT", "type": int, "help": "每轮最多检查的市场数量"},
    "dual_wallet_min_seconds_before_start": {"default": 30, "env": "SIMMER_FASTLOOP_DUAL_WALLET_MIN_SECONDS_BEFORE_START", "type": int, "help": "距离事件开始至少还需保留多少秒才允许挂初始单"},
    "candidate_journal": {"default": False, "env": "SIMMER_FASTLOOP_CANDIDATE_JOURNAL", "type": bool, "help": "是否将候选决策写入 JSONL 日志，便于回放分析"},
    "candidate_journal_file": {"default": "candidate_journal.jsonl", "env": "SIMMER_FASTLOOP_CANDIDATE_JOURNAL_FILE", "type": str, "help": "候选决策日志文件路径"},
    "polymarket_accounts": {"default": [], "env": "SIMMER_FASTLOOP_POLYMARKET_ACCOUNTS", "type": list, "help": "结构化的 Polymarket 多账户配置"},
    "execution_route": {"default": None, "env": "SIMMER_FASTLOOP_EXECUTION_ROUTE", "type": str, "help": "实盘执行通道：direct_clob 或 simmer_wallet"},
    "order_type": {"default": "GTC", "env": "SIMMER_FASTLOOP_ORDER_TYPE", "type": str, "help": "订单类型：GTC、FAK、FOK、GTD（默认 GTC）"},
    "dual_wallet_dry_run_status_script": {"default": {}, "env": "SIMMER_FASTLOOP_DUAL_WALLET_DRY_RUN_STATUS_SCRIPT", "type": dict, "help": "dry_run 下按账号/side脚本化返回订单状态，用于无真实下单验证 Step 6/7/8"},
    "dual_wallet_mock_mode": {"default": False, "env": "SIMMER_FASTLOOP_DUAL_WALLET_MOCK_MODE", "type": bool, "help": "Mock 模式：模拟整个流程但不真实下单"},
    "dual_wallet_mock_fill_side": {"default": "UP", "env": "SIMMER_FASTLOOP_DUAL_WALLET_MOCK_FILL_SIDE", "type": str, "help": "Mock 模式下模拟哪侧先成交：UP 或 DOWN"},
    "dual_wallet_mock_fill_after_sec": {"default": 5, "env": "SIMMER_FASTLOOP_DUAL_WALLET_MOCK_FILL_AFTER_SEC", "type": int, "help": "Mock 模式下模拟成交延迟（秒）"},
    # ===== 全局事件日志配置 =====
    "global_event_journal_enabled": {"default": True, "env": "SIMMER_FASTLOOP_GLOBAL_EVENT_JOURNAL_ENABLED", "type": bool, "help": "是否启用全局事件日志（跨会话持久化到固定文件）"},
    "global_event_journal_file": {"default": "main/global_trade_events.json", "env": "SIMMER_FASTLOOP_GLOBAL_EVENT_JOURNAL_FILE", "type": str, "help": "全局事件日志文件路径"},
    "global_event_journal_flush_interval": {"default": 5, "env": "SIMMER_FASTLOOP_GLOBAL_EVENT_JOURNAL_FLUSH_INTERVAL", "type": int, "help": "全局事件日志刷新间隔（秒）"},
}

WALLET_LINK_RETRIES = int(os.environ.get("SIMMER_WALLET_LINK_RETRIES", "4"))
WALLET_LINK_RETRY_DELAY = float(os.environ.get("SIMMER_WALLET_LINK_RETRY_DELAY", "2"))
DIRECT_POLYMARKET_CLOB = os.environ.get("SIMMER_FASTLOOP_DIRECT_CLOB", "true").lower() in ("true", "1", "yes", "on")
DIRECT_CLOB_HOST = os.environ.get("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com")
DIRECT_CLOB_CHAIN_ID = int(os.environ.get("POLYMARKET_CHAIN_ID", "137"))
DIRECT_CLOB_SIGNATURE_TYPE = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "1"))
DIRECT_CLOB_FUNDER = os.environ.get("POLYMARKET_FUNDER")


def _resolve_config_path(skill_file: str | os.PathLike[str]) -> Path:
    script_path = Path(skill_file).resolve()
    search_dirs = [script_path.parent, *script_path.parents]
    matches: list[Path] = []
    for directory in search_dirs:
        candidate = directory / "config.json"
        if candidate.exists():
            matches.append(candidate)
    if not matches:
        return script_path.parent / "config.json"
    for candidate in matches:
        try:
            payload = json.loads(candidate.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and any(key in payload for key in CONFIG_SCHEMA):
            return candidate
    return matches[0]


def load_config(skill_file):
    try:
        from simmer_sdk.skill import load_config as sdk_load_config
        sdk_cfg = sdk_load_config(CONFIG_SCHEMA, skill_file, slug="polymarket-fast-loop")
        if isinstance(sdk_cfg, dict) and sdk_cfg.get("polymarket_accounts"):
            return sdk_cfg
    except ImportError:
        pass

    cfg = {key: meta["default"] for key, meta in CONFIG_SCHEMA.items()}
    config_path = _resolve_config_path(skill_file)
    if config_path.exists():
        try:
            file_cfg = json.loads(config_path.read_text())
            if isinstance(file_cfg, dict):
                cfg.update({k: v for k, v in file_cfg.items() if k in CONFIG_SCHEMA and not str(k).startswith("_")})
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Warning: could not read {config_path}: {exc}", file=sys.stderr)
    for key, meta in CONFIG_SCHEMA.items():
        env_name = meta.get("env")
        if not env_name or env_name not in os.environ:
            continue
        raw = os.environ[env_name]
        type_fn = meta.get("type", str)
        try:
            if type_fn == bool:
                cfg[key] = raw.lower() in ("true", "1", "yes", "on")
            elif type_fn == list:
                cfg[key] = json.loads(raw)
            else:
                cfg[key] = type_fn(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            print(f"Warning: invalid env value for {env_name}: {raw}", file=sys.stderr)
    return cfg


def get_config_path(skill_file):
    try:
        from simmer_sdk.skill import get_config_path as sdk_get_config_path
        return sdk_get_config_path(skill_file)
    except ImportError:
        return str(Path(skill_file).resolve().parent / "config.json")


def update_config(updates, skill_file):
    try:
        from simmer_sdk.skill import update_config as sdk_update_config
        return sdk_update_config(updates, skill_file)
    except ImportError:
        config_path = Path(get_config_path(skill_file))
        existing = {}
        if config_path.exists():
            try:
                existing = json.loads(config_path.read_text())
                if not isinstance(existing, dict):
                    existing = {}
            except (OSError, json.JSONDecodeError):
                existing = {}
        existing.update({k: v for k, v in updates.items() if k in CONFIG_SCHEMA})
        config_path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n")
        return existing


def resolve_config(skill_file):
    cfg = load_config(skill_file)
    global STRATEGY_MODE, EXECUTION_ROUTE, ORDER_TYPE
    global POLYMARKET_ACCOUNTS
    global DUAL_WALLET_ENTRY_TIMEOUT_SEC, DUAL_WALLET_FORCE_CLOSE_WINDOW_SEC
    global DUAL_WALLET_FIXED_SELL_PRICE, DUAL_WALLET_ENTRY_SHARES
    global DUAL_WALLET_FAK_CLOSE_PRICE, DUAL_WALLET_FOK_SELL_PRICE, DUAL_WALLET_FOK_SELL_PRICE_BASE, DUAL_WALLET_FOK_SELL_PRICE_DECAY_WINDOW_SEC, DUAL_WALLET_FOK_INTERVAL_SEC
    global DUAL_WALLET_ENTRY_UP_PRICE, DUAL_WALLET_ENTRY_DOWN_PRICE
    global DUAL_WALLET_MAX_CONSECUTIVE_LOSSES, DUAL_WALLET_POLL_INTERVAL_SEC
    global DUAL_WALLET_EVENT_QUERY_LIMIT, DUAL_WALLET_MIN_SECONDS_BEFORE_START
    global CANDIDATE_JOURNAL, CANDIDATE_JOURNAL_FILE, DUAL_WALLET_DRY_RUN_STATUS_SCRIPT
    global GLOBAL_EVENT_JOURNAL_ENABLED, GLOBAL_EVENT_JOURNAL_FILE

    STRATEGY_MODE = cfg.get("strategy_mode", "dual_wallet_event").lower()
    route = cfg.get("execution_route")
    if not route:
        route = "direct_clob" if DIRECT_POLYMARKET_CLOB else "simmer_wallet"
        cfg["execution_route"] = route
    EXECUTION_ROUTE = route.lower().replace("-", "_")
    cfg["execution_route"] = EXECUTION_ROUTE
    ORDER_TYPE = cfg.get("order_type", "GTC").upper()
    cfg["order_type"] = ORDER_TYPE

    POLYMARKET_ACCOUNTS = cfg.get("polymarket_accounts", [])
    DUAL_WALLET_ENTRY_TIMEOUT_SEC = cfg.get("dual_wallet_entry_timeout_sec", 100)
    DUAL_WALLET_FORCE_CLOSE_WINDOW_SEC = cfg.get("dual_wallet_force_close_window_sec", 60)
    DUAL_WALLET_FIXED_SELL_PRICE = cfg.get("dual_wallet_fixed_sell_price", 0.76)
    DUAL_WALLET_FAK_CLOSE_PRICE = cfg.get("dual_wallet_fak_close_price", 0.99)
    DUAL_WALLET_FOK_SELL_PRICE = cfg.get("dual_wallet_fok_sell_price", 0.01)
    DUAL_WALLET_FOK_SELL_PRICE_BASE = cfg.get("dual_wallet_fok_sell_price_base", None)
    DUAL_WALLET_FOK_SELL_PRICE_DECAY_WINDOW_SEC = cfg.get("dual_wallet_fok_sell_price_decay_window_sec", None)
    DUAL_WALLET_FOK_INTERVAL_SEC = cfg.get("dual_wallet_fok_interval_sec", 3.0)
    DUAL_WALLET_ENTRY_UP_PRICE = cfg.get("dual_wallet_entry_up_price", 0.5)
    DUAL_WALLET_ENTRY_DOWN_PRICE = cfg.get("dual_wallet_entry_down_price", 0.5)
    DUAL_WALLET_ENTRY_SHARES = cfg.get("dual_wallet_entry_shares", 10.0)
    DUAL_WALLET_MAX_CONSECUTIVE_LOSSES = cfg.get("dual_wallet_max_consecutive_losses", 2)
    DUAL_WALLET_POLL_INTERVAL_SEC = cfg.get("dual_wallet_poll_interval_sec", 5)
    DUAL_WALLET_EVENT_QUERY_LIMIT = cfg.get("dual_wallet_event_query_limit", 20)
    DUAL_WALLET_MIN_SECONDS_BEFORE_START = cfg.get("dual_wallet_min_seconds_before_start", 60)
    DUAL_WALLET_DRY_RUN_STATUS_SCRIPT = cfg.get("dual_wallet_dry_run_status_script", {})
    CANDIDATE_JOURNAL = cfg.get("candidate_journal", False)
    CANDIDATE_JOURNAL_FILE = cfg.get("candidate_journal_file", "candidate_journal.jsonl")
    GLOBAL_EVENT_JOURNAL_ENABLED = cfg.get("global_event_journal_enabled", True)
    GLOBAL_EVENT_JOURNAL_FILE = cfg.get("global_event_journal_file", "main/global_trade_events.json")

    return cfg


resolve_config(__file__)
