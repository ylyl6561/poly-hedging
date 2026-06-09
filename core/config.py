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
    "strategy_mode": {"default": "dual_wallet_event", "env": "SIMMER_FASTLOOP_STRATEGY_MODE", "type": str, "help": "Strategy mode: dual_wallet_event"},
    "dual_wallet_entry_timeout_sec": {"default": 100, "env": "SIMMER_FASTLOOP_DUAL_WALLET_ENTRY_TIMEOUT_SEC", "type": int, "help": "Timeout window for one-side fill handling"},
    "dual_wallet_force_close_window_sec": {"default": 40, "env": "SIMMER_FASTLOOP_DUAL_WALLET_FORCE_CLOSE_WINDOW_SEC", "type": int, "help": "Seconds before close to force liquidation"},
    "dual_wallet_fixed_sell_price": {"default": 0.76, "env": "SIMMER_FASTLOOP_DUAL_WALLET_FIXED_SELL_PRICE", "type": float, "help": "Fixed sell/close price for the first version"},
    "dual_wallet_entry_up_price": {"default": 0.5, "env": "SIMMER_FASTLOOP_DUAL_WALLET_ENTRY_UP_PRICE", "type": float, "help": "Initial UP entry price for dual-wallet event trading"},
    "dual_wallet_entry_down_price": {"default": 0.5, "env": "SIMMER_FASTLOOP_DUAL_WALLET_ENTRY_DOWN_PRICE", "type": float, "help": "Initial DOWN entry price for dual-wallet event trading"},
    "dual_wallet_entry_amount_usd": {"default": 10.0, "env": "SIMMER_FASTLOOP_DUAL_WALLET_ENTRY_AMOUNT_USD", "type": float, "help": "Per-wallet entry amount"},
    "dual_wallet_max_consecutive_losses": {"default": 2, "env": "SIMMER_FASTLOOP_DUAL_WALLET_MAX_CONSECUTIVE_LOSSES", "type": int, "help": "Stop trading after this many consecutive losing events"},
    "dual_wallet_poll_interval_sec": {"default": 5, "env": "SIMMER_FASTLOOP_DUAL_WALLET_POLL_INTERVAL_SEC", "type": int, "help": "Event polling interval"},
    "dual_wallet_event_query_limit": {"default": 20, "env": "SIMMER_FASTLOOP_DUAL_WALLET_EVENT_QUERY_LIMIT", "type": int, "help": "Number of markets to inspect per loop"},
    "candidate_journal": {"default": False, "env": "SIMMER_FASTLOOP_CANDIDATE_JOURNAL", "type": bool, "help": "Write candidate decisions to a JSONL journal for replay"},
    "candidate_journal_file": {"default": "candidate_journal.jsonl", "env": "SIMMER_FASTLOOP_CANDIDATE_JOURNAL_FILE", "type": str, "help": "Path to the candidate journal file"},
    "polymarket_accounts": {"default": [], "env": "SIMMER_FASTLOOP_POLYMARKET_ACCOUNTS", "type": list, "help": "Structured Polymarket multi-account configuration"},
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
    global DUAL_WALLET_FIXED_SELL_PRICE, DUAL_WALLET_ENTRY_AMOUNT_USD
    global DUAL_WALLET_MAX_CONSECUTIVE_LOSSES, DUAL_WALLET_POLL_INTERVAL_SEC
    global DUAL_WALLET_EVENT_QUERY_LIMIT, CANDIDATE_JOURNAL, CANDIDATE_JOURNAL_FILE

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
    DUAL_WALLET_FORCE_CLOSE_WINDOW_SEC = cfg.get("dual_wallet_force_close_window_sec", 40)
    DUAL_WALLET_FIXED_SELL_PRICE = cfg.get("dual_wallet_fixed_sell_price", 0.76)
    DUAL_WALLET_ENTRY_UP_PRICE = cfg.get("dual_wallet_entry_up_price", 0.5)
    DUAL_WALLET_ENTRY_DOWN_PRICE = cfg.get("dual_wallet_entry_down_price", 0.5)
    DUAL_WALLET_ENTRY_AMOUNT_USD = cfg.get("dual_wallet_entry_amount_usd", 10.0)
    DUAL_WALLET_MAX_CONSECUTIVE_LOSSES = cfg.get("dual_wallet_max_consecutive_losses", 2)
    DUAL_WALLET_POLL_INTERVAL_SEC = cfg.get("dual_wallet_poll_interval_sec", 5)
    DUAL_WALLET_EVENT_QUERY_LIMIT = cfg.get("dual_wallet_event_query_limit", 20)
    CANDIDATE_JOURNAL = cfg.get("candidate_journal", False)
    CANDIDATE_JOURNAL_FILE = cfg.get("candidate_journal_file", "candidate_journal.jsonl")

    return cfg


resolve_config(__file__)
