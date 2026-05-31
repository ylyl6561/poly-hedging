"""
Polymarket RTDS price collection for oracle-latency experiments.

The RTDS stream provides both Binance-style crypto prices and Chainlink
data-stream prices. This module listens briefly, keeps the freshest tick for
each source, and returns a snapshot suitable for one-cycle strategy runs.

Dual-Track Mode (默认):
- binance:      RTDS Binance (原始数据，与历史保持一致)
- chainlink:    RTDS Chainlink (唯一 Chainlink 来源)
- binance_ws:   直接 Binance WebSocket (新增，用于对比)

使用对比模式：
- 对比两种 Binance 数据源的延迟和价格差异
- 帮助决定是否可以用 binance_ws 替代 binance
"""

import json
import os
import time
import threading
from collections import deque
from dataclasses import dataclass
from typing import Optional, Dict, Any

# Try to import Binance WS for hybrid mode
try:
    from market.binance_ws_prices import BinanceWSManager, get_stream_manager as get_binance_manager
    HAS_BINANCE_WS = True
except ImportError:
    HAS_BINANCE_WS = False


RTDS_ENDPOINT = "wss://ws-live-data.polymarket.com"
RTDS_PERSISTENT = os.environ.get("SIMMER_FASTLOOP_RTDS_PERSISTENT", "true").lower() in ("true", "1", "yes", "on")
RTDS_CONNECT_TIMEOUT = float(os.environ.get("SIMMER_FASTLOOP_RTDS_CONNECT_TIMEOUT_SEC", "5"))
RTDS_CACHE_WARMUP_TIMEOUT = float(os.environ.get("SIMMER_FASTLOOP_RTDS_CACHE_WARMUP_SEC", "2.0"))
RTDS_HISTORY_SECONDS = int(os.environ.get("SIMMER_FASTLOOP_RTDS_HISTORY_SECONDS", "120"))
RTDS_RECONNECT_MIN_DELAY = float(os.environ.get("SIMMER_FASTLOOP_RTDS_RECONNECT_MIN_SEC", "1.0"))
RTDS_RECONNECT_MAX_DELAY = float(os.environ.get("SIMMER_FASTLOOP_RTDS_RECONNECT_MAX_SEC", "10.0"))

# Dual-track mode: run both RTDS and Binance WS, compare in output
RTDS_DUAL_TRACK = os.environ.get("SIMMER_FASTLOOP_RTDS_DUAL_TRACK", "true").lower() in ("true", "1", "yes", "on")

BINANCE_SYMBOLS = {
    "BTC": "btcusdt",
    "ETH": "ethusdt",
    "SOL": "solusdt",
}

CHAINLINK_SYMBOLS = {
    "BTC": "btc/usd",
    "ETH": "eth/usd",
    "SOL": "sol/usd",
}

# Stale tick thresholds (warn if tick age exceeds these values)
STALE_TICK_WARN_BINANCE_MS = int(os.environ.get("SIMMER_FASTLOOP_STALE_WARN_BINANCE_MS", "5000"))
STALE_TICK_WARN_CHAINLINK_MS = int(os.environ.get("SIMMER_FASTLOOP_STALE_WARN_CHAINLINK_MS", "10000"))
STALE_TICK_CRITICAL_MS = int(os.environ.get("SIMMER_FASTLOOP_STALE_CRITICAL_MS", "30000"))

# Auto-recovery thresholds
AUTO_RECONNECT_ON_STALE_MS = int(os.environ.get("SIMMER_FASTLOOP_AUTO_RECONNECT_STALE_MS", "15000"))
AUTO_RECONNECT_CHECK_INTERVAL = 5.0  # Check every 5 seconds

_STREAMS = {}
_STREAMS_LOCK = threading.Lock()

# Singleton Binance manager for hybrid mode
_binance_manager: Optional["BinanceWSManager"] = None
_binance_manager_lock = threading.Lock()


@dataclass
class PriceTick:
    source: str
    symbol: str
    value: float
    timestamp_ms: int
    received_ms: int

    @property
    def age_ms(self):
        return max(0, int(time.time() * 1000) - int(self.timestamp_ms))

    def to_dict(self):
        return {
            "source": self.source,
            "symbol": self.symbol,
            "value": self.value,
            "timestamp_ms": self.timestamp_ms,
            "received_ms": self.received_ms,
            "age_ms": self.age_ms,
        }


def _parse_tick(message, asset):
    if not isinstance(message, dict):
        return None
    topic = message.get("topic")
    payload = message.get("payload")
    if not isinstance(payload, dict):
        return None

    symbol = str(payload.get("symbol") or "").lower()
    wanted_binance = BINANCE_SYMBOLS.get(asset, "btcusdt")
    wanted_chainlink = CHAINLINK_SYMBOLS.get(asset, "btc/usd")

    if topic == "crypto_prices" and symbol == wanted_binance:
        source = "binance"
    elif topic == "crypto_prices_chainlink" and symbol == wanted_chainlink:
        source = "chainlink"
    else:
        return None

    try:
        value = float(payload["value"])
        tick_ts = int(payload.get("timestamp") or message.get("timestamp"))
    except (KeyError, TypeError, ValueError):
        return None

    return PriceTick(
        source=source,
        symbol=symbol,
        value=value,
        timestamp_ms=tick_ts,
        received_ms=int(time.time() * 1000),
    )


def _get_binance_manager(symbol: str = "btcusdt") -> Optional["BinanceWSManager"]:
    """Get or create singleton Binance WebSocket manager."""
    global _binance_manager
    if not HAS_BINANCE_WS:
        return None
    with _binance_manager_lock:
        if _binance_manager is None:
            _binance_manager = BinanceWSManager(
                symbols=[symbol.lower()],
                stream_type="bookTicker",
            )
            _binance_manager.start()
        return _binance_manager


def format_dual_track_comparison(snapshot: Dict[str, Any]) -> str:
    """Format dual-track comparison for logging.

    Args:
        snapshot: Output from collect_rtds_prices()

    Returns:
        str: Formatted comparison string for logging
    """
    comparison = snapshot.get("binance_ws_vs_binance", {})
    if not comparison:
        return ""

    lines = []
    lines.append("=== 双轨 Binance 对比 ===")
    lines.append(f"  RTDS Binance:  ${comparison.get('rtds_value', 0):.2f} (age: {comparison.get('rtds_age_ms', 0)}ms)")
    lines.append(f"  Binance WS:    ${comparison.get('ws_value', 0):.2f} (age: {comparison.get('ws_age_ms', 0)}ms)")
    lines.append(f"  价格差:        ${comparison.get('price_diff', 0):+.2f} ({comparison.get('price_diff_pct', 0):+.4f}%)")
    lines.append(f"  时间戳差:      {comparison.get('timestamp_diff_ms', 0)}ms")
    lines.append(f"  推荐使用:      {comparison.get('winner', 'unknown')}")
    lines.append("=" * 30)

    return "\n".join(lines)


def _convert_binance_tick_to_rtds_format(tick: Dict[str, Any], source: str = "binance") -> Optional[Dict]:
    """Convert Binance tick format to RTDS-compatible format."""
    if not tick:
        return None
    try:
        return {
            "source": source,
            "symbol": tick.get("symbol", ""),
            "value": float(tick.get("mid_price") or tick.get("price", 0)),
            "timestamp_ms": int(tick.get("timestamp_ms", time.time() * 1000)),
            "received_ms": int(tick.get("received_ms", time.time() * 1000)),
            "age_ms": int(tick.get("age_ms", 0)),
        }
    except (TypeError, ValueError):
        return None


def check_tick_health(snapshot):
    """Check if RTDS ticks are stale and return health status with warnings.

    Enhanced version with dual-track comparison.

    Returns:
        dict: {
            "healthy": bool,              # True if all ticks are fresh
            "warnings": list,             # List of warning strings
            "actions": list,              # Recommended actions to recover
            "binance_age_ms": int,       # Age of binance (RTDS) tick in ms
            "chainlink_age_ms": int,     # Age of chainlink tick in ms
            "binance_ws_age_ms": int,    # Age of binance_ws tick in ms
            "dual_track": bool,           # Whether dual-track mode is enabled
            "recovery_needed": bool,      # True if auto-recovery should trigger
        }
    """
    warnings = []
    actions = []
    recovery_needed = False
    binance_tick = snapshot.get("binance")
    chainlink_tick = snapshot.get("chainlink")
    binance_ws_tick = snapshot.get("binance_ws")

    binance_age_ms = binance_tick.get("age_ms", 0) if binance_tick else None
    chainlink_age_ms = chainlink_tick.get("age_ms", 0) if chainlink_tick else None
    binance_ws_age_ms = binance_ws_tick.get("age_ms", 0) if binance_ws_tick else None
    dual_track = snapshot.get("status", {}).get("dual_track", False)
    comparison = snapshot.get("binance_ws_vs_binance", {})

    # Check RTDS Binance tick
    if binance_age_ms is None:
        warnings.append("⚠️ Binance (RTDS) tick: MISSING (no data received)")
        recovery_needed = True
    elif binance_age_ms >= STALE_TICK_CRITICAL_MS:
        warnings.append(f"🚨 Binance (RTDS) tick: CRITICAL stale ({binance_age_ms/1000:.1f}s old)")
        recovery_needed = True
    elif binance_age_ms >= STALE_TICK_WARN_BINANCE_MS:
        warnings.append(f"⚡ Binance (RTDS) tick: stale ({binance_age_ms/1000:.1f}s old)")

    # Check Binance WS tick (if dual-track enabled)
    if dual_track:
        if binance_ws_age_ms is None:
            warnings.append("⚠️ Binance (WS) tick: MISSING (direct WS not receiving data)")
            actions.append("Check Binance WS connection")
        elif binance_ws_age_ms >= STALE_TICK_CRITICAL_MS:
            warnings.append(f"🚨 Binance (WS) tick: CRITICAL stale ({binance_ws_age_ms/1000:.1f}s)")
            recovery_needed = True
        elif binance_ws_age_ms >= STALE_TICK_WARN_BINANCE_MS:
            warnings.append(f"⚡ Binance (WS) tick: stale ({binance_ws_age_ms/1000:.1f}s)")

        # Dual-track comparison
        if comparison:
            winner = comparison.get("winner", "")
            ts_diff = comparison.get("timestamp_diff_ms", 0)
            price_diff = comparison.get("price_diff", 0)
            if winner:
                warnings.append(f"ℹ️ Dual-track winner: {winner} (by {(comparison.get(f'{winner}_age_ms', 0) or 0):.0f}ms)")
            if abs(price_diff) > 1.0:  # $1 difference
                warnings.append(f"⚠️ Binance price diff: ${price_diff:+.2f} (RTDS vs WS)")

    # Check Chainlink tick
    if chainlink_age_ms is None:
        warnings.append("⚠️ Chainlink tick: MISSING (no data received)")
        recovery_needed = True
    elif chainlink_age_ms >= STALE_TICK_CRITICAL_MS:
        warnings.append(f"🚨 Chainlink tick: CRITICAL stale ({chainlink_age_ms/1000:.1f}s old)")
        recovery_needed = True
    elif chainlink_age_ms >= STALE_TICK_WARN_CHAINLINK_MS:
        warnings.append(f"⚡ Chainlink tick: stale ({chainlink_age_ms/1000:.1f}s old)")

    healthy = len(warnings) == 0 or all("⚠️" in w and "🚨" not in w for w in warnings)

    return {
        "healthy": healthy,
        "warnings": warnings,
        "actions": actions,
        "recovery_needed": recovery_needed,
        "binance_age_ms": binance_age_ms,
        "chainlink_age_ms": chainlink_age_ms,
        "binance_ws_age_ms": binance_ws_age_ms,
        "dual_track": dual_track,
        "comparison": comparison,
    }


def force_stream_reconnect(asset: str = "BTC"):
    """Force reconnect RTDS stream for a specific asset."""
    asset = (asset or "BTC").upper()
    key = (asset, RTDS_ENDPOINT)
    with _STREAMS_LOCK:
        stream = _STREAMS.pop(key, None)
    if stream:
        stream.stop()
        time.sleep(0.5)
        # Start fresh
        return start_rtds_stream(asset=asset, endpoint=RTDS_ENDPOINT)
    return start_rtds_stream(asset=asset, endpoint=RTDS_ENDPOINT)


def _subscription_message():
    return json.dumps({
        "action": "subscribe",
        "subscriptions": [
            {
                "topic": "crypto_prices",
                "type": "update",
            },
            {
                "topic": "crypto_prices_chainlink",
                "type": "*",
                "filters": "",
            },
        ],
    })


class _PersistentRTDSStream:
    """Background RTDS WebSocket reader with latest-tick snapshots."""

    def __init__(self, asset, endpoint):
        self.asset = (asset or "BTC").upper()
        self.endpoint = endpoint
        self.latest = {}
        self.samples = {"binance": deque(), "chainlink": deque()}
        self.error = None
        self.connected = False
        self.started_at_ms = None
        self.last_message_ms = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        # Auto-recovery state
        self._last_binance_tick_ms = None
        self._last_chainlink_tick_ms = None
        self._stale_count = 0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"rtds-{self.asset.lower()}",
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _remember_tick(self, tick):
        tick_dict = tick.to_dict()
        cutoff_ms = int(time.time() * 1000) - (RTDS_HISTORY_SECONDS * 1000)
        now_ms = int(time.time() * 1000)
        with self._lock:
            self.latest[tick.source] = tick
            self.samples[tick.source].append(tick_dict)
            for source in ("binance", "chainlink"):
                while self.samples[source] and int(self.samples[source][0].get("received_ms", 0)) < cutoff_ms:
                    self.samples[source].popleft()
            self.last_message_ms = tick.received_ms
            # Track last tick time per source for staleness detection
            if tick.source == "binance":
                self._last_binance_tick_ms = now_ms
            elif tick.source == "chainlink":
                self._last_chainlink_tick_ms = now_ms
            # Reset stale count on any tick
            self._stale_count = 0

    def _check_staleness(self) -> bool:
        """Check if we haven't received ticks for too long. Returns True if stale."""
        now_ms = int(time.time() * 1000)
        stale = False
        with self._lock:
            if self._last_binance_tick_ms is not None:
                age = now_ms - self._last_binance_tick_ms
                if age > AUTO_RECONNECT_ON_STALE_MS:
                    stale = True
            if self._last_chainlink_tick_ms is not None:
                age = now_ms - self._last_chainlink_tick_ms
                if age > AUTO_RECONNECT_ON_STALE_MS:
                    stale = True
        return stale

    def force_reconnect(self):
        """Force a reconnection of the stream."""
        self._stale_count += 1
        print(f"[RTDS] Force reconnect requested (stale count: {self._stale_count})")
        # The _run loop will handle reconnection on next exception

    def _run(self):
        try:
            import websocket
        except ImportError as exc:
            with self._lock:
                self.error = f"websocket-client not installed: {exc}"
            return

        reconnect_delay = RTDS_RECONNECT_MIN_DELAY
        consecutive_timeouts = 0
        max_consecutive_timeouts = 3  # Force reconnect after 3 consecutive timeouts

        while not self._stop.is_set():
            ws = None
            try:
                ws = websocket.create_connection(self.endpoint, timeout=RTDS_CONNECT_TIMEOUT)
                ws.settimeout(1.0)
                ws.send(_subscription_message())
                with self._lock:
                    self.connected = True
                    self.error = None
                    self.started_at_ms = int(time.time() * 1000)
                reconnect_delay = RTDS_RECONNECT_MIN_DELAY
                consecutive_timeouts = 0

                last_ping = 0.0
                last_staleness_check = 0.0

                while not self._stop.is_set():
                    now = time.monotonic()

                    # Periodic staleness check
                    if now - last_staleness_check >= AUTO_RECONNECT_CHECK_INTERVAL:
                        if self._check_staleness():
                            print(f"[RTDS] Staleness detected, triggering reconnect...")
                            raise Exception("Stale stream detected")
                        last_staleness_check = now

                    if now - last_ping >= 5:
                        try:
                            ws.send("PING")
                        except Exception:
                            pass
                        last_ping = now

                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        consecutive_timeouts += 1
                        if consecutive_timeouts >= max_consecutive_timeouts:
                            print(f"[RTDS] {consecutive_timeouts} consecutive timeouts, reconnecting...")
                            raise Exception("Too many timeouts")
                        continue
                    except Exception as exc:
                        raise exc

                    consecutive_timeouts = 0
                    if not raw or raw == "PONG":
                        continue
                    try:
                        message = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    tick = _parse_tick(message, self.asset)
                    if tick:
                        self._remember_tick(tick)
            except Exception as exc:
                with self._lock:
                    self.connected = False
                    self.error = str(exc)
                print(f"[RTDS] Connection error: {exc}")
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass

            if not self._stop.is_set():
                time.sleep(reconnect_delay)
                reconnect_delay = min(RTDS_RECONNECT_MAX_DELAY, reconnect_delay * 1.5)

    def snapshot(self, sample_seconds=6):
        """Get current snapshot of all data sources.
        
        Returns:
            dict: {
                "binance": RTDS Binance tick (原始数据),
                "chainlink": RTDS Chainlink tick,
                "binance_ws": Direct Binance WS tick (双轨模式),
                "binance_ws_vs_binance": 对比数据 (双轨模式),
                ...
            }
        """
        cutoff_ms = int(time.time() * 1000) - max(1, int(sample_seconds or 1)) * 1000

        with self._lock:
            latest = {
                source: tick.to_dict()
                for source, tick in self.latest.items()
                if tick is not None
            }
            samples = {
                source: [
                    dict(item)
                    for item in self.samples[source]
                    if int(item.get("received_ms", 0)) >= cutoff_ms
                ]
                for source in ("binance", "chainlink")
            }
            status = {
                "persistent": True,
                "connected": self.connected,
                "error": self.error,
                "last_message_ms": self.last_message_ms,
                "started_at_ms": self.started_at_ms,
                "dual_track": RTDS_DUAL_TRACK,
            }

        result = {
            "binance": latest.get("binance"),  # 原始 RTDS Binance
            "chainlink": latest.get("chainlink"),  # RTDS Chainlink
            "samples": samples,
            "error": status["error"],
            "status": status,
        }

        # 双轨模式：同时获取直接 Binance WS 数据
        if RTDS_DUAL_TRACK and HAS_BINANCE_WS:
            binance_ws_tick = self._get_binance_ws_tick()
            result["binance_ws"] = binance_ws_tick

            # 计算 binance_ws 与 binance 的对比
            if binance_ws_tick and latest.get("binance"):
                rtds_binance = latest.get("binance")
                result["binance_ws_vs_binance"] = self._compute_binance_comparison(
                    binance_ws_tick, rtds_binance
                )

        return result

    def _get_binance_ws_tick(self) -> Optional[Dict[str, Any]]:
        """Get latest tick from direct Binance WebSocket."""
        try:
            manager = _get_binance_manager(BINANCE_SYMBOLS.get(self.asset, "btcusdt"))
            if manager:
                tick = manager.get_latest(BINANCE_SYMBOLS.get(self.asset, "btcusdt"))
                return _convert_binance_tick_to_rtds_format(tick, "binance_ws")
        except Exception as e:
            print(f"[RTDS] Failed to get Binance WS tick: {e}")
        return None

    def _compute_binance_comparison(
        self,
        binance_ws: Dict[str, Any],
        binance_rtds: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Compute comparison between direct Binance WS and RTDS Binance."""
        if not binance_ws or not binance_rtds:
            return {}

        ws_value = binance_ws.get("value", 0)
        rtds_value = binance_rtds.get("value", 0)
        ws_ts = binance_ws.get("timestamp_ms", 0)
        rtds_ts = binance_rtds.get("timestamp_ms", 0)

        return {
            "price_diff": ws_value - rtds_value,
            "price_diff_pct": ((ws_value - rtds_value) / rtds_value * 100) if rtds_value else 0,
            "timestamp_diff_ms": ws_ts - rtds_ts,  # 正数表示 ws 更新
            "ws_age_ms": binance_ws.get("age_ms", 0),
            "rtds_age_ms": binance_rtds.get("age_ms", 0),
            "ws_value": ws_value,
            "rtds_value": rtds_value,
            "winner": "binance_ws" if binance_ws.get("age_ms", float("inf")) < binance_rtds.get("age_ms", float("inf")) else "binance",
        }


def start_rtds_stream(asset="BTC", endpoint=RTDS_ENDPOINT):
    """Start or return the persistent RTDS stream for an asset."""
    asset = (asset or "BTC").upper()
    key = (asset, endpoint)
    with _STREAMS_LOCK:
        stream = _STREAMS.get(key)
        if stream is None:
            stream = _PersistentRTDSStream(asset, endpoint)
            _STREAMS[key] = stream
        stream.start()
        return stream


def stop_rtds_stream(asset="BTC", endpoint=RTDS_ENDPOINT):
    asset = (asset or "BTC").upper()
    key = (asset, endpoint)
    with _STREAMS_LOCK:
        stream = _STREAMS.pop(key, None)
    if stream:
        stream.stop()


def _collect_rtds_prices_oneshot(asset="BTC", sample_seconds=6, endpoint=RTDS_ENDPOINT):
    """Original one-shot RTDS sampler used as a fallback or when persistence is disabled."""
    asset = (asset or "BTC").upper()
    sample_seconds = max(1, int(sample_seconds or 1))

    try:
        import websocket
    except ImportError as exc:
        return {"error": f"websocket-client not installed: {exc}"}

    latest = {}
    samples = {"binance": [], "chainlink": []}
    ws = None
    deadline = time.monotonic() + sample_seconds
    try:
        ws = websocket.create_connection(endpoint, timeout=RTDS_CONNECT_TIMEOUT)
        ws.send(_subscription_message())

        last_ping = 0.0
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now - last_ping >= 5:
                try:
                    ws.send("PING")
                except Exception:
                    pass
                last_ping = now

            ws.settimeout(max(0.2, min(1.0, deadline - now)))
            try:
                raw = ws.recv()
            except Exception:
                continue
            if not raw or raw == "PONG":
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            tick = _parse_tick(message, asset)
            if tick:
                latest[tick.source] = tick
                samples[tick.source].append(tick.to_dict())
    except Exception as exc:
        latest["error"] = str(exc)
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    return {
        "binance": latest.get("binance").to_dict() if latest.get("binance") else None,
        "chainlink": latest.get("chainlink").to_dict() if latest.get("chainlink") else None,
        "samples": samples,
        "error": latest.get("error"),
        "status": {"persistent": False},
    }


def collect_rtds_prices(asset="BTC", sample_seconds=6, endpoint=RTDS_ENDPOINT):
    """Collect latest Binance and Chainlink ticks from Polymarket RTDS.

    By default this reads a persistent background WebSocket and returns a
    latest-tick snapshot plus recent in-memory samples. That avoids creating a
    new TLS/WebSocket connection for every strategy evaluation.

    Dual-Track Mode (默认启用):
    - binance:      RTDS Binance (原始数据，与历史保持一致)
    - chainlink:    RTDS Chainlink
    - binance_ws:   直接 Binance WebSocket (新增，用于对比)
    - binance_ws_vs_binance: 对比结果

    Returns:
        dict: {
            "binance": {...},           # RTDS Binance tick
            "chainlink": {...},         # RTDS Chainlink tick
            "binance_ws": {...},        # 直接 Binance WS tick (双轨模式)
            "binance_ws_vs_binance": {...},  # 对比数据 (双轨模式)
            "samples": {...},           # 历史样本
            "status": {...},            # 连接状态
            "_health": {...},           # 健康检查结果
        }
    """
    asset = (asset or "BTC").upper()
    sample_seconds = max(1, int(sample_seconds or 1))

    if not RTDS_PERSISTENT:
        return _collect_rtds_prices_oneshot(asset=asset, sample_seconds=sample_seconds, endpoint=endpoint)

    stream = start_rtds_stream(asset=asset, endpoint=endpoint)
    deadline = time.monotonic() + max(0.0, RTDS_CACHE_WARMUP_TIMEOUT)
    snapshot = stream.snapshot(sample_seconds=sample_seconds)

    # Wait for initial data with timeout
    wait_count = 0
    max_wait = int(RTDS_CACHE_WARMUP_TIMEOUT / 0.05)
    while time.monotonic() < deadline and (not snapshot.get("binance") or not snapshot.get("chainlink")):
        if wait_count >= max_wait:
            break
        time.sleep(0.05)
        snapshot = stream.snapshot(sample_seconds=sample_seconds)
        wait_count += 1

    # Check health and trigger recovery if needed
    health = check_tick_health(snapshot)
    if health.get("recovery_needed", False):
        print(f"[RTDS] Recovery needed: {health.get('actions', [])}")
        force_stream_reconnect(asset=asset)
        time.sleep(0.5)
        snapshot = stream.snapshot(sample_seconds=sample_seconds)
        health = check_tick_health(snapshot)

    snapshot["_health"] = health

    return snapshot


def collect_rtds_prices_with_retry(
    asset="BTC",
    sample_seconds=6,
    max_retries=3,
    retry_delay=1.0,
) -> Dict[str, Any]:
    """Collect RTDS prices with automatic retry on failure.

    This is the recommended entry point for production use.

    Output includes dual-track comparison when enabled.
    """
    last_error = None
    for attempt in range(max_retries):
        result = collect_rtds_prices(asset=asset, sample_seconds=sample_seconds)

        # Check if we have valid data
        health = result.get("_health", {})
        if not health.get("recovery_needed", False):
            return result

        # Check if data is usable
        if result.get("binance") and result.get("chainlink"):
            binance_age = result["binance"].get("age_ms", 0)
            chainlink_age = result["chainlink"].get("age_ms", 0)
            if binance_age < STALE_TICK_CRITICAL_MS and chainlink_age < STALE_TICK_CRITICAL_MS:
                return result

        last_error = result.get("error")
        print(f"[RTDS] Attempt {attempt + 1}/{max_retries} failed, retrying in {retry_delay}s...")
        time.sleep(retry_delay)

    return {
        **result,
        "_retry_warning": f"Failed after {max_retries} attempts. Last error: {last_error}",
    }
