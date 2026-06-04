"""
Configuration management for the fastloop trading system.

This module defines the runtime config schema and resolves module-level values
at import time for both the fastloop and preopen paths.
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
    "strategy_mode": {"default": "dual_wallet_event", "env": "SIMMER_FASTLOOP_STRATEGY_MODE", "type": str, "help": "Strategy mode: dual_wallet_event"},
    "dual_wallet_wallet_a_private_key_env": {"default": "WALLET_PRIVATE_KEY_A", "env": "SIMMER_FASTLOOP_DUAL_WALLET_WALLET_A_PRIVATE_KEY_ENV", "type": str, "help": "Env var name for wallet A private key"},
    "dual_wallet_wallet_b_private_key_env": {"default": "WALLET_PRIVATE_KEY_B", "env": "SIMMER_FASTLOOP_DUAL_WALLET_WALLET_B_PRIVATE_KEY_ENV", "type": str, "help": "Env var name for wallet B private key"},
    "dual_wallet_entry_timeout_sec": {"default": 100, "env": "SIMMER_FASTLOOP_DUAL_WALLET_ENTRY_TIMEOUT_SEC", "type": int, "help": "Timeout window for one-side fill handling"},
    "dual_wallet_force_close_window_sec": {"default": 40, "env": "SIMMER_FASTLOOP_DUAL_WALLET_FORCE_CLOSE_WINDOW_SEC", "type": int, "help": "Seconds before close to force liquidation"},
    "dual_wallet_fixed_sell_price": {"default": 0.76, "env": "SIMMER_FASTLOOP_DUAL_WALLET_FIXED_SELL_PRICE", "type": float, "help": "Fixed sell/close price for the first version"},
    "dual_wallet_entry_amount_usd": {"default": 10.0, "env": "SIMMER_FASTLOOP_DUAL_WALLET_ENTRY_AMOUNT_USD", "type": float, "help": "Per-wallet entry amount"},
    "dual_wallet_max_consecutive_losses": {"default": 2, "env": "SIMMER_FASTLOOP_DUAL_WALLET_MAX_CONSECUTIVE_LOSSES", "type": int, "help": "Stop trading after this many consecutive losing events"},
    "dual_wallet_poll_interval_sec": {"default": 5, "env": "SIMMER_FASTLOOP_DUAL_WALLET_POLL_INTERVAL_SEC", "type": int, "help": "Event polling interval"},
    "dual_wallet_event_query_limit": {"default": 20, "env": "SIMMER_FASTLOOP_DUAL_WALLET_EVENT_QUERY_LIMIT", "type": int, "help": "Number of markets to inspect per loop"},
    "candidate_journal": {"default": False, "env": "SIMMER_FASTLOOP_CANDIDATE_JOURNAL", "type": bool, "help": "Write candidate decisions to a JSONL journal for replay"},
    "candidate_journal_file": {"default": "candidate_journal.jsonl", "env": "SIMMER_FASTLOOP_CANDIDATE_JOURNAL_FILE", "type": str, "help": "Path to the candidate journal file"},
    "preopen_poll_interval_sec": {"default": 5, "env": "SIMMER_PREOPEN_POLL_INTERVAL_SEC", "type": int, "help": "Polling interval for preopen loop"},
    "preopen_lead_time_sec": {"default": 300, "env": "SIMMER_PREOPEN_LEAD_TIME_SEC", "type": int, "help": "Lead time before open to begin preopen actions"},
    "preopen_gc_grace_sec": {"default": 30, "env": "SIMMER_PREOPEN_GC_GRACE_SEC", "type": int, "help": "Grace period before garbage-collecting stale preopen state"},
    "preopen_yes_shares_x": {"default": 10.0, "env": "SIMMER_PREOPEN_YES_SHARES_X", "type": float, "help": "Base YES share size for preopen entries"},
    "preopen_yes_max_price": {"default": 0.8, "env": "SIMMER_PREOPEN_YES_MAX_PRICE", "type": float, "help": "Maximum YES entry price"},
    "preopen_hedge_ratio": {"default": 1.0, "env": "SIMMER_PREOPEN_HEDGE_RATIO", "type": float, "help": "Hedge-size multiplier for paired legs"},
    "preopen_down_resting_price": {"default": 0.4, "env": "SIMMER_PREOPEN_DOWN_RESTING_PRICE", "type": float, "help": "Resting NO hedge price"},
    "preopen_down_switch_ttl_sec": {"default": 40, "env": "SIMMER_PREOPEN_DOWN_SWITCH_TTL_SEC", "type": int, "help": "Seconds before open to switch NO resting to FAK"},
    "preopen_down_entry_max_price": {"default": 0.42, "env": "SIMMER_PREOPEN_DOWN_ENTRY_MAX_PRICE", "type": float, "help": "Maximum price for NO entry"},
    "preopen_down_fak_max_price": {"default": 0.42, "env": "SIMMER_PREOPEN_DOWN_FAK_MAX_PRICE", "type": float, "help": "Maximum price for NO FAK switch"},
    "preopen_min_arb_edge": {"default": 0.01, "env": "SIMMER_PREOPEN_MIN_ARB_EDGE", "type": float, "help": "Minimum arbitrage edge required"},
    "preopen_max_actions_per_event": {"default": 4, "env": "SIMMER_PREOPEN_MAX_ACTIONS_PER_EVENT", "type": int, "help": "Max actions allowed per event"},
    "execution_route": {"default": None, "env": "SIMMER_FASTLOOP_EXECUTION_ROUTE", "type": str, "help": "Live execution route: direct_clob or simmer_wallet"},
    "order_type": {"default": "GTC", "env": "SIMMER_FASTLOOP_ORDER_TYPE", "type": str, "help": "Order type: GTC, FAK, FOK, GTD (default: GTC)"},
}

WALLET_LINK_RETRIES = int(os.environ.get("SIMMER_WALLET_LINK_RETRIES", "4"))
WALLET_LINK_RETRY_DELAY = float(os.environ.get("SIMMER_WALLET_LINK_RETRY_DELAY", "2"))
DIRECT_POLYMARKET_CLOB = os.environ.get("SIMMER_FASTLOOP_DIRECT_CLOB", "true").lower() in ("true", "1", "yes", "on")
DIRECT_CLOB_HOST = os.environ.get("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com")
DIRECT_CLOB_CHAIN_ID = int(os.environ.get("POLYMARKET_CHAIN_ID", "137"))
DIRECT_CLOB_SIGNATURE_TYPE = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "1"))
DIRECT_CLOB_FUNDER = os.environ.get("POLYMARKET_FUNDER")


def load_config(skill_file):
    try:
        from simmer_sdk.skill import load_config as sdk_load_config
        return sdk_load_config(CONFIG_SCHEMA, skill_file, slug="polymarket-fast-loop")
    except ImportError:
        cfg = {key: meta["default"] for key, meta in CONFIG_SCHEMA.items()}
        config_path = Path(skill_file).resolve().parent / "config.json"
        if config_path.exists():
            try:
                file_cfg = json.loads(config_path.read_text())
                if isinstance(file_cfg, dict):
                    cfg.update({k: v for k, v in file_cfg.items() if k in CONFIG_SCHEMA})
            except (OSError, json.JSONDecodeError) as exc:
                print(f"Warning: could not read {config_path}: {exc}", file=sys.stderr)
        for key, meta in CONFIG_SCHEMA.items():
            env_name = meta.get("env")
            if not env_name or env_name not in os.environ:
                continue
            raw = os.environ[env_name]
            type_fn = meta.get("type", str)
            try:
                cfg[key] = raw.lower() in ("true", "1", "yes", "on") if type_fn == bool else type_fn(raw)
            except (TypeError, ValueError):
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
    global DUAL_WALLET_WALLET_A_PRIVATE_KEY_ENV, DUAL_WALLET_WALLET_B_PRIVATE_KEY_ENV
    global DUAL_WALLET_ENTRY_TIMEOUT_SEC, DUAL_WALLET_FORCE_CLOSE_WINDOW_SEC
    global DUAL_WALLET_FIXED_SELL_PRICE, DUAL_WALLET_ENTRY_AMOUNT_USD
    global DUAL_WALLET_MAX_CONSECUTIVE_LOSSES, DUAL_WALLET_POLL_INTERVAL_SEC
    global DUAL_WALLET_EVENT_QUERY_LIMIT, CANDIDATE_JOURNAL, CANDIDATE_JOURNAL_FILE
    global PREOPEN_POLL_INTERVAL_SEC, PREOPEN_LEAD_TIME_SEC, PREOPEN_GC_GRACE_SEC
    global PREOPEN_YES_SHARES_X, PREOPEN_YES_MAX_PRICE, PREOPEN_HEDGE_RATIO
    global PREOPEN_DOWN_RESTING_PRICE, PREOPEN_DOWN_SWITCH_TTL_SEC
    global PREOPEN_DOWN_ENTRY_MAX_PRICE, PREOPEN_DOWN_FAK_MAX_PRICE
    global PREOPEN_MIN_ARB_EDGE, PREOPEN_MAX_ACTIONS_PER_EVENT

    STRATEGY_MODE = cfg.get("strategy_mode", "dual_wallet_event").lower()
    route = cfg.get("execution_route")
    if not route:
        route = "direct_clob" if DIRECT_POLYMARKET_CLOB else "simmer_wallet"
        cfg["execution_route"] = route
    EXECUTION_ROUTE = route.lower().replace("-", "_")
    cfg["execution_route"] = EXECUTION_ROUTE
    ORDER_TYPE = cfg.get("order_type", "GTC").upper()
    cfg["order_type"] = ORDER_TYPE

    DUAL_WALLET_WALLET_A_PRIVATE_KEY_ENV = cfg.get("dual_wallet_wallet_a_private_key_env", "WALLET_PRIVATE_KEY_A")
    DUAL_WALLET_WALLET_B_PRIVATE_KEY_ENV = cfg.get("dual_wallet_wallet_b_private_key_env", "WALLET_PRIVATE_KEY_B")
    DUAL_WALLET_ENTRY_TIMEOUT_SEC = cfg.get("dual_wallet_entry_timeout_sec", 100)
    DUAL_WALLET_FORCE_CLOSE_WINDOW_SEC = cfg.get("dual_wallet_force_close_window_sec", 40)
    DUAL_WALLET_FIXED_SELL_PRICE = cfg.get("dual_wallet_fixed_sell_price", 0.76)
    DUAL_WALLET_ENTRY_AMOUNT_USD = cfg.get("dual_wallet_entry_amount_usd", 10.0)
    DUAL_WALLET_MAX_CONSECUTIVE_LOSSES = cfg.get("dual_wallet_max_consecutive_losses", 2)
    DUAL_WALLET_POLL_INTERVAL_SEC = cfg.get("dual_wallet_poll_interval_sec", 5)
    DUAL_WALLET_EVENT_QUERY_LIMIT = cfg.get("dual_wallet_event_query_limit", 20)
    CANDIDATE_JOURNAL = cfg.get("candidate_journal", False)
    CANDIDATE_JOURNAL_FILE = cfg.get("candidate_journal_file", "candidate_journal.jsonl")
    PREOPEN_POLL_INTERVAL_SEC = cfg.get("preopen_poll_interval_sec", 5)
    PREOPEN_LEAD_TIME_SEC = cfg.get("preopen_lead_time_sec", 300)
    PREOPEN_GC_GRACE_SEC = cfg.get("preopen_gc_grace_sec", 30)
    PREOPEN_YES_SHARES_X = cfg.get("preopen_yes_shares_x", 10.0)
    PREOPEN_YES_MAX_PRICE = cfg.get("preopen_yes_max_price", 0.8)
    PREOPEN_HEDGE_RATIO = cfg.get("preopen_hedge_ratio", 1.0)
    PREOPEN_DOWN_RESTING_PRICE = cfg.get("preopen_down_resting_price", 0.4)
    PREOPEN_DOWN_SWITCH_TTL_SEC = cfg.get("preopen_down_switch_ttl_sec", 40)
    PREOPEN_DOWN_ENTRY_MAX_PRICE = cfg.get("preopen_down_entry_max_price", 0.42)
    PREOPEN_DOWN_FAK_MAX_PRICE = cfg.get("preopen_down_fak_max_price", 0.42)
    PREOPEN_MIN_ARB_EDGE = cfg.get("preopen_min_arb_edge", 0.01)
    PREOPEN_MAX_ACTIONS_PER_EVENT = cfg.get("preopen_max_actions_per_event", 4)

    return cfg


resolve_config(__file__)
