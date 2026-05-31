"""
Binance WebSocket 价格数据获取模块

直接连接 Binance WebSocket 获取实时价格数据，绕过 Polymarket RTDS。
优势：
- 延迟更低（<100ms vs Polymarket 的 1-2s）
- 无频率限制（服务器主动推送，最快 1000ms 推送一次）
- 更稳定可靠（直接从源头获取）

支持的 Stream：
- <symbol>@ticker: 实时推送最新盘口、价格和量能数据
- <symbol>@bookTicker: 最佳买一/卖一价格和挂单量（最快，延迟最低）
"""

import json
import os
import time
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Callable

try:
    import websocket
    HAS_WEBSOCKET = True
except ImportError:
    HAS_WEBSOCKET = False


# =============================================================================
# Configuration
# =============================================================================

BINANCE_WS_ENDPOINT = "wss://stream.binance.com:9443/ws"
BINANCE_WS_ENDPOINT_COMBINED = "wss://stream.binance.com:9443/stream"

# 连接配置
WS_CONNECT_TIMEOUT = float(os.environ.get("BINANCE_WS_CONNECT_TIMEOUT_SEC", "5"))
WS_RECONNECT_MIN_DELAY = float(os.environ.get("BINANCE_WS_RECONNECT_MIN_SEC", "1.0"))
WS_RECONNECT_MAX_DELAY = float(os.environ.get("BINANCE_WS_RECONNECT_MAX_SEC", "30.0"))
WS_PING_INTERVAL = float(os.environ.get("BINANCE_WS_PING_INTERVAL_SEC", "30"))
WS_PING_TIMEOUT = float(os.environ.get("BINANCE_WS_PING_TIMEOUT_SEC", "10"))

# 历史数据保留
WS_HISTORY_SECONDS = int(os.environ.get("BINANCE_WS_HISTORY_SECONDS", "300"))

# 符号映射
ASSET_TO_SYMBOL = {
    "BTC": "btcusdt",
    "ETH": "ethusdt",
    "SOL": "solusdt",
}


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class BinanceTick:
    """单个价格tick数据"""
    symbol: str                    # 符号 (btcusdt)
    price: float                  # 当前价格
    price_change: float           # 24小时价格变化
    price_change_pct: float       # 24小时价格变化百分比
    volume: float                # 24小时成交量
    quote_volume: float           # 24小时成交额
    high_24h: float              # 24小时最高价
    low_24h: float               # 24小时最低价
    bid_price: float              # 买一价
    bid_qty: float               # 买一量
    ask_price: float             # 卖一价
    ask_qty: float               # 卖一量
    timestamp_ms: int            # Binance 服务器时间戳
    received_ms: int             # 本地接收时间戳

    @property
    def age_ms(self) -> int:
        """Tick 年龄（毫秒）"""
        return max(0, int(time.time() * 1000) - self.timestamp_ms)

    @property
    def spread(self) -> float:
        """买卖价差"""
        return self.ask_price - self.bid_price

    @property
    def mid_price(self) -> float:
        """中间价"""
        return (self.ask_price + self.bid_price) / 2

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "price": self.price,
            "price_change": self.price_change,
            "price_change_pct": self.price_change_pct,
            "volume": self.volume,
            "quote_volume": self.quote_volume,
            "high_24h": self.high_24h,
            "low_24h": self.low_24h,
            "bid_price": self.bid_price,
            "bid_qty": self.bid_qty,
            "ask_price": self.ask_price,
            "ask_qty": self.ask_qty,
            "timestamp_ms": self.timestamp_ms,
            "received_ms": self.received_ms,
            "age_ms": self.age_ms,
            "spread": self.spread,
            "mid_price": self.mid_price,
        }


@dataclass
class BookTickerTick:
    """最佳买卖盘口数据（最低延迟）"""
    symbol: str
    bid_price: float
    bid_qty: float
    ask_price: float
    ask_qty: float
    timestamp_ms: int
    received_ms: int

    @property
    def age_ms(self) -> int:
        return max(0, int(time.time() * 1000) - self.timestamp_ms)

    @property
    def spread(self) -> float:
        return self.ask_price - self.bid_price

    @property
    def mid_price(self) -> float:
        return (self.ask_price + self.bid_price) / 2

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "bid_price": self.bid_price,
            "bid_qty": self.bid_qty,
            "ask_price": self.ask_price,
            "ask_qty": self.ask_qty,
            "timestamp_ms": self.timestamp_ms,
            "received_ms": self.received_ms,
            "age_ms": self.age_ms,
            "spread": self.spread,
            "mid_price": self.mid_price,
        }


# =============================================================================
# WebSocket Stream Manager
# =============================================================================

class BinanceWSManager:
    """
    Binance WebSocket 管理器

    支持多种模式：
    1. 单 Symbol 单 Stream: btcusdt@ticker
    2. 单 Symbol BookTicker: btcusdt@bookTicker (最快)
    3. 多 Symbol 组合 Stream
    """

    def __init__(
        self,
        symbols: list = None,
        stream_type: str = "bookTicker",  # "ticker" | "bookTicker" | "kline_1m"
        use_combined: bool = True,
        on_tick: Callable = None,
    ):
        """
        Args:
            symbols: 交易对列表，如 ["btcusdt", "ethusdt"]
            stream_type: 数据流类型
                - "bookTicker": 最佳买卖盘（最低延迟，推荐）
                - "ticker": 完整行情数据
                - "kline_1m": 1分钟K线
            use_combined: 是否使用组合流（一个连接订阅多个stream）
            on_tick: 收到tick时的回调函数
        """
        if not HAS_WEBSOCKET:
            raise RuntimeError("websocket-client 库未安装: pip install websocket-client")

        self.symbols = [s.lower() for s in (symbols or ["btcusdt"])]
        self.stream_type = stream_type
        self.use_combined = use_combined
        self.on_tick = on_tick

        self._ws = None
        self._thread = None
        self._running = False
        self._reconnect_delay = WS_RECONNECT_MIN_DELAY

        # 数据存储
        self._latest: Dict[str, Any] = {}
        self._history: Dict[str, deque] = {s: deque(maxlen=1000) for s in self.symbols}
        self._lock = threading.Lock()

        # 统计
        self._msg_count = 0
        self._last_msg_ms = None
        self._connected = False
        self._error = None

    def _get_stream_name(self, symbol: str) -> str:
        """获取 stream 名称"""
        stream_map = {
            "bookTicker": f"{symbol}@bookTicker",
            "ticker": f"{symbol}@ticker",
            "kline_1m": f"{symbol}@kline_1m",
        }
        return stream_map.get(self.stream_type, f"{symbol}@bookTicker")

    def _get_ws_url(self) -> str:
        """获取 WebSocket URL"""
        if self.use_combined and len(self.symbols) > 1:
            streams = "/".join([self._get_stream_name(s) for s in self.symbols])
            return f"{BINANCE_WS_ENDPOINT_COMBINED}?streams={streams}"
        elif len(self.symbols) == 1:
            return f"{BINANCE_WS_ENDPOINT}/{self._get_stream_name(self.symbols[0])}"
        else:
            streams = "/".join([self._get_stream_name(s) for s in self.symbols])
            return f"{BINANCE_WS_ENDPOINT}/{streams}"

    def _parse_ticker(self, data: Dict) -> Optional[BinanceTick]:
        """解析 ticker 数据"""
        try:
            s = data.get("s", "").lower()
            return BinanceTick(
                symbol=s,
                price=float(data.get("c", 0)),
                price_change=float(data.get("p", 0)),
                price_change_pct=float(data.get("P", 0)),
                volume=float(data.get("v", 0)),
                quote_volume=float(data.get("q", 0)),
                high_24h=float(data.get("h", 0)),
                low_24h=float(data.get("l", 0)),
                bid_price=float(data.get("b", 0)),
                bid_qty=float(data.get("B", 0)),
                ask_price=float(data.get("a", 0)),
                ask_qty=float(data.get("A", 0)),
                timestamp_ms=int(data.get("E", time.time() * 1000)),
                received_ms=int(time.time() * 1000),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _parse_book_ticker(self, data: Dict) -> Optional[BookTickerTick]:
        """解析 bookTicker 数据"""
        try:
            symbol = data.get("s", "").lower()
            return BookTickerTick(
                symbol=symbol,
                bid_price=float(data.get("b", 0)),
                bid_qty=float(data.get("B", 0)),
                ask_price=float(data.get("a", 0)),
                ask_qty=float(data.get("A", 0)),
                timestamp_ms=int(data.get("E", time.time() * 1000)),
                received_ms=int(time.time() * 1000),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _parse_kline(self, data: Dict) -> Optional[Dict]:
        """解析 kline 数据"""
        try:
            k = data.get("k", {})
            return {
                "symbol": k.get("s", "").lower(),
                "open_time": int(k.get("t", 0)),
                "open": float(k.get("o", 0)),
                "high": float(k.get("h", 0)),
                "low": float(k.get("l", 0)),
                "close": float(k.get("c", 0)),
                "volume": float(k.get("v", 0)),
                "close_time": int(k.get("T", 0)),
                "is_closed": bool(k.get("x", False)),
                "timestamp_ms": int(data.get("E", time.time() * 1000)),
                "received_ms": int(time.time() * 1000),
            }
        except (KeyError, TypeError, ValueError):
            return None

    def _on_message(self, ws, message):
        """收到消息"""
        self._msg_count += 1
        self._last_msg_ms = int(time.time() * 1000)
        self._error = None

        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return

        # 处理组合流格式
        if isinstance(data, dict) and "stream" in data and "data" in data:
            stream = data.get("stream", "")
            data = data.get("data", {})
            # 从 stream 名推断 symbol
            for sym in self.symbols:
                if sym in stream:
                    symbol = sym
                    break
            else:
                return
        else:
            symbol = data.get("s", "").lower() if isinstance(data, dict) else None
            if symbol not in self.symbols:
                return

        # 解析数据
        tick = None
        if self.stream_type == "ticker":
            tick = self._parse_ticker(data)
        elif self.stream_type == "bookTicker":
            tick = self._parse_book_ticker(data)
        elif self.stream_type == "kline_1m":
            tick = self._parse_kline(data)

        if tick:
            with self._lock:
                self._latest[symbol] = tick
                self._history[symbol].append(tick.to_dict() if hasattr(tick, 'to_dict') else tick)

            if self.on_tick:
                self.on_tick(symbol, tick)

    def _on_error(self, ws, error):
        """错误处理"""
        self._error = str(error)
        print(f"[BinanceWS] Error: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        """连接关闭"""
        self._connected = False
        print(f"[BinanceWS] Connection closed: {close_status_code} - {close_msg}")

    def _on_open(self, ws):
        """连接打开"""
        self._connected = True
        self._error = None
        self._reconnect_delay = WS_RECONNECT_MIN_DELAY
        print(f"[BinanceWS] Connected to {self._get_ws_url()}")

    def _run_loop(self):
        """WebSocket 主循环"""
        while self._running:
            try:
                url = self._get_ws_url()
                self._ws = websocket.WebSocketApp(
                    url,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                    on_open=self._on_open,
                )

                # 运行 WebSocket
                self._ws.run_forever(
                    ping_interval=WS_PING_INTERVAL,
                    ping_timeout=WS_PING_TIMEOUT,
                    reconnect=0,  # 我们自己处理重连
                )

            except Exception as e:
                self._error = str(e)
                print(f"[BinanceWS] Connection error: {e}")

            if self._running:
                print(f"[BinanceWS] Reconnecting in {self._reconnect_delay:.1f}s...")
                time.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    WS_RECONNECT_MAX_DELAY,
                    self._reconnect_delay * 1.5
                )

    def start(self):
        """启动 WebSocket 连接"""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"BinanceWS-{'-'.join(self.symbols)}",
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        """停止 WebSocket 连接"""
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def get_latest(self, symbol: str = None) -> Optional[Dict]:
        """获取最新 tick"""
        with self._lock:
            if symbol:
                tick = self._latest.get(symbol.lower())
                return tick.to_dict() if tick else None
            else:
                return {
                    sym: tick.to_dict() if tick else None
                    for sym, tick in self._latest.items()
                }

    def get_history(self, symbol: str, seconds: int = None) -> list:
        """获取历史 tick 数据"""
        with self._lock:
            history = list(self._history.get(symbol.lower(), []))
        
        if seconds:
            cutoff_ms = int(time.time() * 1000) - seconds * 1000
            history = [h for h in history if h.get("received_ms", 0) >= cutoff_ms]
        
        return history

    def get_stats(self) -> Dict:
        """获取连接统计"""
        with self._lock:
            latest_count = len([t for t in self._latest.values() if t is not None])
        
        return {
            "running": self._running,
            "connected": self._connected,
            "error": self._error,
            "msg_count": self._msg_count,
            "last_msg_ms": self._last_msg_ms,
            "symbols": self.symbols,
            "stream_type": self.stream_type,
            "symbols_with_data": latest_count,
        }

    def snapshot(self, sample_seconds: int = 60) -> Dict:
        """获取数据快照（兼容现有接口）"""
        stats = self.get_stats()
        latest = self.get_latest()
        
        # 获取最近 N 秒的数据
        recent_history = {}
        for symbol in self.symbols:
            recent_history[symbol] = self.get_history(symbol, seconds=sample_seconds)
        
        return {
            "binance": latest.get(self.symbols[0]) if latest else None,
            "all_symbols": latest,
            "history": recent_history,
            "stats": stats,
            "timestamp_ms": int(time.time() * 1000),
        }


# =============================================================================
# Singleton Stream Manager
# =============================================================================

_stream_manager: Optional[BinanceWSManager] = None
_stream_lock = threading.Lock()


def get_stream_manager(
    symbols: list = None,
    stream_type: str = "bookTicker",
) -> BinanceWSManager:
    """获取单例的 WebSocket 管理器"""
    global _stream_manager
    
    with _stream_lock:
        if _stream_manager is None:
            _stream_manager = BinanceWSManager(
                symbols=symbols or ["btcusdt"],
                stream_type=stream_type,
            )
            _stream_manager.start()
        return _stream_manager


def stop_stream_manager():
    """停止并清理单例"""
    global _stream_manager
    with _stream_lock:
        if _stream_manager:
            _stream_manager.stop()
            _stream_manager = None


# =============================================================================
# 便捷函数
# =============================================================================

def get_binance_price(symbol: str = "BTCUSDT") -> Optional[float]:
    """
    获取单个币种的当前价格（最简单接口）
    
    Args:
        symbol: 交易对符号，如 "BTCUSDT"
    
    Returns:
        当前价格，或 None
    """
    manager = get_stream_manager(symbols=[symbol.lower()], stream_type="bookTicker")
    
    # 等待初始数据
    for _ in range(50):  # 最多等 5 秒
        tick = manager.get_latest(symbol.lower())
        if tick and tick.get("mid_price"):
            return tick.get("mid_price")
        time.sleep(0.1)
    
    return None


def get_binance_book_ticker(symbol: str = "BTCUSDT") -> Optional[Dict]:
    """
    获取最佳买卖盘口数据
    
    Returns:
        包含 bid_price, ask_price, bid_qty, ask_qty, spread, mid_price 的字典
    """
    manager = get_stream_manager(symbols=[symbol.lower()], stream_type="bookTicker")
    
    for _ in range(50):
        tick = manager.get_latest(symbol.lower())
        if tick:
            return tick
        time.sleep(0.1)
    
    return None


def collect_binance_prices(
    symbols: list = None,
    sample_seconds: float = 2.0,
    stream_type: str = "bookTicker",
) -> Dict:
    """
    收集一段时间的价格数据（兼容现有接口）
    
    Args:
        symbols: 交易对列表
        sample_seconds: 采样时长（秒）
        stream_type: 数据流类型
    
    Returns:
        包含最新价格和历史数据的字典
    """
    symbols = symbols or ["BTCUSDT"]
    manager = get_stream_manager(
        symbols=[s.lower() for s in symbols],
        stream_type=stream_type,
    )
    
    # 等待采样时间
    time.sleep(sample_seconds)
    
    return manager.snapshot(sample_seconds=int(sample_seconds))


# =============================================================================
# 测试代码
# =============================================================================

if __name__ == "__main__":
    import sys
    
    def on_tick(symbol, tick):
        print(f"[{symbol}] {tick}")
    
    print("="*60)
    print("Binance WebSocket 价格获取测试")
    print("="*60)
    
    # 测试 1: BookTicker 模式
    print("\n📡 测试 BookTicker 模式 (最低延迟)...")
    manager = BinanceWSManager(
        symbols=["btcusdt", "ethusdt"],
        stream_type="bookTicker",
        on_tick=on_tick,
    )
    manager.start()
    
    print("等待数据...")
    for i in range(10):
        time.sleep(1)
        snapshot = manager.snapshot()
        print(f"\n[{i+1}s] 状态: {snapshot['stats']}")
        
        for sym in ["btcusdt", "ethusdt"]:
            tick = manager.get_latest(sym)
            if tick:
                print(f"  {sym.upper()}: bid={tick['bid_price']:.2f} ask={tick['ask_price']:.2f} spread={tick['spread']:.4f}")
    
    manager.stop()
    
    # 测试 2: Ticker 模式
    print("\n" + "="*60)
    print("📡 测试 Ticker 模式 (完整行情)...")
    manager2 = BinanceWSManager(
        symbols=["btcusdt"],
        stream_type="ticker",
        on_tick=on_tick,
    )
    manager2.start()
    
    time.sleep(3)
    tick = manager2.get_latest("btcusdt")
    if tick:
        print(f"  BTCUSDT: price={tick['price']:.2f} 24h_change={tick['price_change_pct']:.2f}%")
        print(f"  high={tick['high_24h']:.2f} low={tick['low_24h']:.2f}")
        print(f"  volume={tick['volume']:.4f} quote_volume={tick['quote_volume']:.2f}")
    
    manager2.stop()
    
    print("\n✅ 测试完成")
