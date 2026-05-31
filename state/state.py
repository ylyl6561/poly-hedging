"""
State management for the fastloop trading system.

Handles:
- Daily spend tracking (budget caps per UTC day)
- Entered markets dedup (local same-day marker)
- Candidate journal (JSONL for offline replay)

All state is persisted to JSON files next to fastloop_trader.py.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from core import load_env_file, CANDIDATE_JOURNAL, CANDIDATE_JOURNAL_FILE


def open_sign(value, eps=0.001):
    """Classify a numeric value: 1 (positive), -1 (negative), 0 (near-zero)."""
    if value > eps:
        return 1
    if value < -eps:
        return -1
    return 0


# =============================================================================
# Paths
# =============================================================================

def _skill_dir(skill_file):
    return Path(skill_file).resolve().parent


def _spend_path(skill_file):
    return _skill_dir(skill_file) / "daily_spend.json"


def _entered_markets_path(skill_file):
    return _skill_dir(skill_file) / "entered_markets.json"


def _market_quality_path(skill_file):
    return _skill_dir(skill_file) / "market_quality_state.json"


def _candidate_journal_path(skill_file):
    journal_path = Path(CANDIDATE_JOURNAL_FILE).expanduser()
    if journal_path.is_absolute():
        return journal_path
    return _skill_dir(skill_file) / journal_path


def _oracle_open_path(skill_file):
    return _skill_dir(skill_file) / "oracle_open_prices.json"


def _oracle_samples_path(skill_file):
    return _skill_dir(skill_file) / "oracle_price_samples.json"


# =============================================================================
# Daily Spend Tracking
# =============================================================================

def load_daily_spend(skill_file):
    """Load today's spend. Resets if date != today (UTC)."""
    path = _spend_path(skill_file)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if path.exists():
        try:
            with open(path) as f:
                data = json.load(f)
            if data.get("date") == today:
                return data
        except (json.JSONDecodeError, IOError):
            pass
    return {"date": today, "spent": 0.0, "trades": 0}


def save_daily_spend(skill_file, spend_data):
    """Save daily spend to file."""
    with open(_spend_path(skill_file), "w") as f:
        json.dump(spend_data, f, indent=2)


# =============================================================================
# Entered Markets Dedup
# =============================================================================

def load_entered_markets(skill_file):
    """Load markets already entered today. Resets if date != today (UTC)."""
    path = _entered_markets_path(skill_file)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if path.exists():
        try:
            with open(path) as f:
                data = json.load(f)
            if data.get("date") == today:
                data.setdefault("markets", [])
                return data
        except (json.JSONDecodeError, IOError):
            pass
    return {"date": today, "markets": []}


def save_entered_markets(skill_file, entered_data):
    with open(_entered_markets_path(skill_file), "w") as f:
        json.dump(entered_data, f, indent=2)


def mark_market_entered(skill_file, entry_key):
    """Record that we entered a market today (dedup marker)."""
    data = load_entered_markets(skill_file)
    markets = data.setdefault("markets", [])
    if entry_key not in markets:
        markets.append(entry_key)
        save_entered_markets(skill_file, data)


def has_entered_market(skill_file, entry_key):
    """Return True if we already entered this market today."""
    data = load_entered_markets(skill_file)
    return entry_key in data.get("markets", [])


# =============================================================================
# Market Quality State (Chop Filter)
# =============================================================================

def load_market_quality_state(skill_file):
    """Load today's chop-filter observations. Resets if date != today (UTC)."""
    path = _market_quality_path(skill_file)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if path.exists():
        try:
            with open(path) as f:
                data = json.load(f)
            if data.get("date") == today:
                data.setdefault("markets", {})
                return data
        except (json.JSONDecodeError, IOError):
            pass
    return {"date": today, "markets": {}}


def save_market_quality_state(skill_file, state):
    path = _market_quality_path(skill_file)
    try:
        with open(path, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
    except IOError:
        pass


def update_market_quality_state(skill_file, market_key, signed_from_open_pct,
                                momentum_pct=None, market_yes_price=None):
    """Append an observation for a market window, tracking sign-flips and max distance."""
    state = load_market_quality_state(skill_file)
    markets = state.setdefault("markets", {})
    now_iso = datetime.now(timezone.utc).isoformat()

    entry = markets.setdefault(market_key, {
        "observations": 0,
        "sign_flips": 0,
        "last_sign": 0,
        "max_abs_distance_pct": 0.0,
        "first_seen": now_iso,
        "samples": [],
    })

    sign = open_sign(signed_from_open_pct)
    last_sign = int(entry.get("last_sign") or 0)
    if sign and last_sign and sign != last_sign:
        entry["sign_flips"] = int(entry.get("sign_flips", 0)) + 1
    if sign:
        entry["last_sign"] = sign

    entry["observations"] = int(entry.get("observations", 0)) + 1
    entry["max_abs_distance_pct"] = max(
        float(entry.get("max_abs_distance_pct", 0.0)),
        abs(float(signed_from_open_pct)),
    )
    entry["last_signed_from_open_pct"] = round(float(signed_from_open_pct), 6)
    entry["last_seen"] = now_iso

    sample = {
        "observed_at": now_iso,
        "signed_from_open_pct": round(float(signed_from_open_pct), 6),
        "abs_distance_pct": round(abs(float(signed_from_open_pct)), 6),
        "sign": sign,
    }
    if momentum_pct is not None:
        sample["momentum_pct"] = round(float(momentum_pct), 6)
    if market_yes_price is not None:
        sample["market_yes_price"] = round(float(market_yes_price), 6)

    samples = entry.setdefault("samples", [])
    samples.append(sample)
    entry["samples"] = samples[-12:]

    markets[market_key] = entry
    save_market_quality_state(skill_file, state)
    return dict(entry)


# =============================================================================
# Candidate Journal (Replay)
# =============================================================================

def append_candidate_record(skill_file, record):
    """Append one fair-value candidate decision as JSONL for offline replay."""
    if not CANDIDATE_JOURNAL:
        return
    record = dict(record)
    record.setdefault("recorded_at", datetime.now(timezone.utc).isoformat())
    journal_path = _candidate_journal_path(skill_file)
    try:
        with open(journal_path, "a") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")
    except IOError:
        pass


# =============================================================================
# Oracle Open Prices
# =============================================================================

def load_oracle_open_prices(skill_file):
    """Load captured Chainlink price-to-beat values for today's fast markets."""
    path = _oracle_open_path(skill_file)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if path.exists():
        try:
            with open(path) as f:
                data = json.load(f)
            if data.get("date") == today:
                data.setdefault("markets", {})
                return data
        except (json.JSONDecodeError, IOError):
            pass
    return {"date": today, "markets": {}}


def save_oracle_open_prices(skill_file, data):
    try:
        with open(_oracle_open_path(skill_file), "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
    except IOError:
        pass


def get_oracle_open_price(skill_file, market_key):
    data = load_oracle_open_prices(skill_file)
    entry = data.get("markets", {}).get(str(market_key))
    if not entry:
        return None
    try:
        return float(entry["price"])
    except (KeyError, TypeError, ValueError):
        return None


def save_oracle_open_price(skill_file, market_key, price, source_timestamp_ms=None):
    data = load_oracle_open_prices(skill_file)
    markets = data.setdefault("markets", {})
    key = str(market_key)
    if key in markets:
        return markets[key]

    entry = {
        "price": float(price),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "source": "chainlink_rtds",
    }
    if source_timestamp_ms is not None:
        entry["source_timestamp_ms"] = int(source_timestamp_ms)
    markets[key] = entry
    save_oracle_open_prices(skill_file, data)
    return entry


def load_oracle_price_samples(skill_file):
    path = _oracle_samples_path(skill_file)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if path.exists():
        try:
            with open(path) as f:
                data = json.load(f)
            if data.get("date") == today:
                data.setdefault("samples", [])
                return data
        except (json.JSONDecodeError, IOError):
            pass
    return {"date": today, "samples": []}


def save_oracle_price_samples(skill_file, data):
    try:
        with open(_oracle_samples_path(skill_file), "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
    except IOError:
        pass


def append_oracle_price_sample(skill_file, asset, binance_tick, chainlink_tick,
                               retention_minutes=90):
    """Persist RTDS samples so a later cycle can recover the window-open price."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    retention_ms = max(1, int(retention_minutes or 90)) * 60 * 1000
    data = load_oracle_price_samples(skill_file)
    samples = [
        s for s in data.get("samples", [])
        if now_ms - int(s.get("observed_ms", 0)) <= retention_ms
    ]
    record = {
        "asset": (asset or "BTC").upper(),
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "observed_ms": now_ms,
    }
    if binance_tick:
        record["binance_price"] = float(binance_tick["value"])
        record["binance_timestamp_ms"] = int(binance_tick["timestamp_ms"])
    if chainlink_tick:
        record["chainlink_price"] = float(chainlink_tick["value"])
        record["chainlink_timestamp_ms"] = int(chainlink_tick["timestamp_ms"])
    samples.append(record)
    data["samples"] = samples[-5000:]
    save_oracle_price_samples(skill_file, data)
    return record


def append_oracle_price_samples(skill_file, asset, samples, retention_minutes=90):
    """Persist multiple RTDS samples from one websocket collection window."""
    if not samples:
        return []
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    retention_ms = max(1, int(retention_minutes or 90)) * 60 * 1000
    data = load_oracle_price_samples(skill_file)
    existing = [
        s for s in data.get("samples", [])
        if now_ms - int(s.get("observed_ms", 0)) <= retention_ms
    ]
    wanted_asset = (asset or "BTC").upper()
    records = []
    binance_samples = samples.get("binance", []) if isinstance(samples, dict) else []
    chainlink_samples = samples.get("chainlink", []) if isinstance(samples, dict) else []

    for tick in binance_samples:
        try:
            records.append({
                "asset": wanted_asset,
                "observed_at": datetime.fromtimestamp(int(tick["received_ms"]) / 1000, tz=timezone.utc).isoformat(),
                "observed_ms": int(tick["received_ms"]),
                "binance_price": float(tick["value"]),
                "binance_timestamp_ms": int(tick["timestamp_ms"]),
            })
        except (KeyError, TypeError, ValueError):
            continue

    for tick in chainlink_samples:
        try:
            records.append({
                "asset": wanted_asset,
                "observed_at": datetime.fromtimestamp(int(tick["received_ms"]) / 1000, tz=timezone.utc).isoformat(),
                "observed_ms": int(tick["received_ms"]),
                "chainlink_price": float(tick["value"]),
                "chainlink_timestamp_ms": int(tick["timestamp_ms"]),
            })
        except (KeyError, TypeError, ValueError):
            continue

    if not records:
        return []
    existing.extend(records)
    data["samples"] = existing[-5000:]
    save_oracle_price_samples(skill_file, data)
    return records


def recover_oracle_open_price(skill_file, asset, open_timestamp_ms, tolerance_seconds=8):
    """Return the nearest local Chainlink sample to the market-open timestamp."""
    tolerance_ms = max(0, int(tolerance_seconds or 0)) * 1000
    if tolerance_ms <= 0:
        return None
    wanted_asset = (asset or "BTC").upper()
    data = load_oracle_price_samples(skill_file)
    best = None
    best_delta = None
    for sample in data.get("samples", []):
        if sample.get("asset") != wanted_asset or sample.get("chainlink_price") is None:
            continue
        ts = sample.get("chainlink_timestamp_ms") or sample.get("observed_ms")
        try:
            delta = abs(int(ts) - int(open_timestamp_ms))
        except (TypeError, ValueError):
            continue
        if delta <= tolerance_ms and (best_delta is None or delta < best_delta):
            best = sample
            best_delta = delta
    if not best:
        return None
    return {
        "price": float(best["chainlink_price"]),
        "delta_ms": int(best_delta),
        "source_timestamp_ms": int(best.get("chainlink_timestamp_ms") or best.get("observed_ms")),
        "source": "local_chainlink_sample",
    }
