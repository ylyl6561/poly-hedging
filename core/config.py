"""
Configuration management for the fastloop trading system.

Loads configuration from:
1. config.json file
2. Environment variables
3. Default values

The primary entry point is load_config() which returns a dict.
"""

import json
import os
import sys
from pathlib import Path

from .constants import WINDOW_SECONDS


def load_env_file(skill_file):
    """Load KEY=VALUE pairs from a local .env file without overriding real env vars.

    Search order (first found wins):
    1) Same directory as skill_file
    2) Parent directories up to repo root (max 4 levels)

    This supports running entrypoints from `main/` while keeping `.env` at repo root.
    """
    start_dir = Path(skill_file).resolve().parent

    candidates: list[Path] = []
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
        if (len(value) >= 2
                and value[0] == value[-1]
                and value[0] in ("'", '"')):
            value = value[1:-1]
        os.environ[key] = value


# Load .env before any other configuration
load_env_file(__file__)


# =============================================================================
# Configuration Schema — preopen_yes_down strategy mode
# =============================================================================

CONFIG_SCHEMA = {
    # ── Strategy mode ──────────────────────────────────────────────────────────
    "strategy_mode": {
        "default": "preopen_yes_down",
        "env": "SIMMER_FASTLOOP_STRATEGY_MODE",
        "type": str,
        "help": "Strategy mode: preopen_yes_down (primary)",
    },

    # ── Polling ────────────────────────────────────────────────────────────────
    "preopen_poll_interval_sec": {
        "default": 60,
        "env": "SIMMER_FASTLOOP_PREOPEN_POLL_INTERVAL_SEC",
        "type": int,
        "help": "Seconds between market pool refresh cycles (default: 60)",
    },
    "preopen_lead_time_sec": {
        "default": 600,
        "env": "SIMMER_FASTLOOP_PREOPEN_LEAD_TIME_SEC",
        "type": int,
        "help": "Include events starting within this many seconds from now (default: 600 = 10min)",
    },
    "preopen_gc_grace_sec": {
        "default": 60,
        "env": "SIMMER_FASTLOOP_PREOPEN_GC_GRACE_SEC",
        "type": int,
        "help": "Grace period before GC removes started/expired events (default: 60s)",
    },

    # ── YES entry ─────────────────────────────────────────────────────────────
    "preopen_yes_shares_x": {
        "default": 10.0,
        "env": "SIMMER_FASTLOOP_PREOPEN_YES_SHARES_X",
        "type": float,
        "help": "Number of YES shares to buy on pre-open (default: 10)",
    },
    "preopen_yes_max_price": {
        "default": 0.92,
        "env": "SIMMER_FASTLOOP_PREOPEN_YES_MAX_PRICE",
        "type": float,
        "help": "Only buy YES if price <= this (default: 0.92)",
    },

    # ── Down hedge ────────────────────────────────────────────────────────────
    "preopen_hedge_ratio": {
        "default": 1.0,
        "env": "SIMMER_FASTLOOP_PREOPEN_HEDGE_RATIO",
        "type": float,
        "help": "Down shares = YES shares * hedge_ratio (default: 1.0)",
    },
    "preopen_down_resting_price": {
        "default": 0.40,
        "env": "SIMMER_FASTLOOP_PREOPEN_DOWN_RESTING_PRICE",
        "type": float,
        "help": "Down GTC PostOnly limit price (default: 0.40)",
    },
    "preopen_down_switch_ttl_sec": {
        "default": 40,
        "env": "SIMMER_FASTLOOP_PREOPEN_DOWN_SWITCH_TTL_SEC",
        "type": int,
        "help": "Switch Down to FAK when time_to_start <= this many seconds (default: 40)",
    },
    "preopen_down_fak_max_price": {
        "default": 0.42,
        "env": "SIMMER_FASTLOOP_PREOPEN_DOWN_FAK_MAX_PRICE",
        "type": float,
        "help": "Down FAK price cap when switching near market open (default: 0.42)",
    },

    # ── Arbitrage gate ────────────────────────────────────────────────────────
    "preopen_min_arb_edge": {
        "default": -0.50,
        "env": "SIMMER_FASTLOOP_PREOPEN_MIN_ARB_EDGE",
        "type": float,
        "help": "Minimum net edge (after fee) to allow FAK switch (default: -0.50, very permissive)",
    },

    # ── Action limits ───────────────────────────────────────────────────────────
    "preopen_max_actions_per_event": {
        "default": 3,
        "env": "SIMMER_FASTLOOP_PREOPEN_MAX_ACTIONS_PER_EVENT",
        "type": int,
        "help": "Max state-transition actions per event to prevent thrashing (default: 3)",
    },
    "live_max_events": {
        "default": None,
        "env": "SIMMER_FASTLOOP_LIVE_MAX_EVENTS",
        "type": int,
        "help": "In live mode, stop after trading this many BTC 5m events (default: unlimited)",
    },

    # ── Settlement reconciliation ─────────────────────────────────────────────
    "settlement_poll_interval_sec": {
        "default": 120,
        "env": "SIMMER_FASTLOOP_SETTLEMENT_POLL_INTERVAL_SEC",
        "type": int,
        "help": "Seconds between settlement reconciliations (default: 120)",
    },

    # ── Execution ─────────────────────────────────────────────────────────────
    "execution_route": {
        "default": None,
        "env": "SIMMER_FASTLOOP_EXECUTION_ROUTE",
        "type": str,
        "help": "Live execution route: direct_clob or simmer_wallet",
    },
    "order_type": {
        "default": "GTC",
        "env": "SIMMER_FASTLOOP_ORDER_TYPE",
        "type": str,
        "help": "Order type: GTC, FAK, FOK, GTD (default: GTC)",
    },

    # ── Candidate journal (still used by state.py) ─────────────────────────────
    "candidate_journal": {
        "default": False,
        "env": "SIMMER_FASTLOOP_CANDIDATE_JOURNAL",
        "type": bool,
        "help": "Write candidate decisions to JSONL for replay",
    },
    "candidate_journal_file": {
        "default": "candidate_journal.jsonl",
        "env": "SIMMER_FASTLOOP_CANDIDATE_JOURNAL_FILE",
        "type": str,
        "help": "Candidate journal JSONL path",
    },
}


# =============================================================================
# Load Configuration
# =============================================================================

WALLET_LINK_RETRIES = int(os.environ.get("SIMMER_WALLET_LINK_RETRIES", "4"))
WALLET_LINK_RETRY_DELAY = float(os.environ.get("SIMMER_WALLET_LINK_RETRY_DELAY", "2"))
DIRECT_POLYMARKET_CLOB = os.environ.get(
    "SIMMER_FASTLOOP_DIRECT_CLOB", "true"
).lower() in ("true", "1", "yes", "on")
DIRECT_CLOB_HOST = os.environ.get("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com")
DIRECT_CLOB_CHAIN_ID = int(os.environ.get("POLYMARKET_CHAIN_ID", "137"))
DIRECT_CLOB_SIGNATURE_TYPE = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))
DIRECT_CLOB_FUNDER = os.environ.get("POLYMARKET_FUNDER")


def load_config(skill_file):
    """Load full config dict using Simmer SDK."""
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
                if type_fn == bool:
                    cfg[key] = raw.lower() in ("true", "1", "yes", "on")
                else:
                    cfg[key] = type_fn(raw)
            except (TypeError, ValueError):
                print(f"Warning: invalid env value for {env_name}: {raw}", file=sys.stderr)
        return cfg


def get_config_path(skill_file):
    """Return the path to the active config file."""
    try:
        from simmer_sdk.skill import get_config_path as sdk_get_config_path
        return sdk_get_config_path(skill_file)
    except ImportError:
        return str(Path(skill_file).resolve().parent / "config.json")


def update_config(updates, skill_file):
    """Update config values via Simmer SDK."""
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
    """
    Load and resolve the full configuration, and populate module-level variables.
    Returns the config dict.
    """
    cfg = load_config(skill_file)

    global STRATEGY_MODE, EXECUTION_ROUTE, ORDER_TYPE
    global PREOPEN_POLL_INTERVAL_SEC, PREOPEN_LEAD_TIME_SEC, PREOPEN_GC_GRACE_SEC
    global PREOPEN_YES_SHARES_X, PREOPEN_YES_MAX_PRICE
    global PREOPEN_HEDGE_RATIO, PREOPEN_DOWN_RESTING_PRICE
    global PREOPEN_DOWN_SWITCH_TTL_SEC, PREOPEN_DOWN_FAK_MAX_PRICE
    global PREOPEN_MIN_ARB_EDGE, PREOPEN_MAX_ACTIONS_PER_EVENT
    global CANDIDATE_JOURNAL, CANDIDATE_JOURNAL_FILE

    STRATEGY_MODE = cfg.get("strategy_mode", "preopen_yes_down").lower()

    route = cfg.get("execution_route")
    if not route:
        route = "direct_clob" if DIRECT_POLYMARKET_CLOB else "simmer_wallet"
        cfg["execution_route"] = route
    EXECUTION_ROUTE = route.lower().replace("-", "_")
    cfg["execution_route"] = EXECUTION_ROUTE
    ORDER_TYPE = cfg.get("order_type", "GTC").upper()
    cfg["order_type"] = ORDER_TYPE

    PREOPEN_POLL_INTERVAL_SEC = cfg.get("preopen_poll_interval_sec", 60)
    PREOPEN_LEAD_TIME_SEC = cfg.get("preopen_lead_time_sec", 600)
    PREOPEN_GC_GRACE_SEC = cfg.get("preopen_gc_grace_sec", 60)

    PREOPEN_YES_SHARES_X = cfg.get("preopen_yes_shares_x", 10.0)
    PREOPEN_YES_MAX_PRICE = cfg.get("preopen_yes_max_price", 0.92)

    PREOPEN_HEDGE_RATIO = cfg.get("preopen_hedge_ratio", 1.0)
    PREOPEN_DOWN_RESTING_PRICE = cfg.get("preopen_down_resting_price", 0.40)
    PREOPEN_DOWN_SWITCH_TTL_SEC = cfg.get("preopen_down_switch_ttl_sec", 40)
    PREOPEN_DOWN_FAK_MAX_PRICE = cfg.get("preopen_down_fak_max_price", 0.42)

    PREOPEN_MIN_ARB_EDGE = cfg.get("preopen_min_arb_edge", -0.50)
    PREOPEN_MAX_ACTIONS_PER_EVENT = cfg.get("preopen_max_actions_per_event", 3)

    CANDIDATE_JOURNAL = cfg.get("candidate_journal", False)
    CANDIDATE_JOURNAL_FILE = cfg.get("candidate_journal_file", "candidate_journal.jsonl")

    return cfg


# Initialize config at module load so that `from config import X` works.
resolve_config(__file__)
