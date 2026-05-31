#!/bin/bash
# Binance WebSocket 价格获取测试脚本
# 运行方式: ./test_binance_ws.sh

cd "$(dirname "$0")"

# 激活虚拟环境
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
else
    echo "错误: 未找到虚拟环境 .venv"
    exit 1
fi

# 清除代理设置（使用直连）
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy

echo "========================================"
echo "Binance WebSocket 价格获取测试"
echo "========================================"
echo "Python: $(which python)"
echo "工作目录: $(pwd)"
echo ""

python -c "
import sys
import time

# 添加当前目录到路径
sys.path.insert(0, '.')

from market.binance_ws_prices import (
    BinanceWSManager,
    get_binance_price,
    get_binance_book_ticker,
)

def on_tick(symbol, tick):
    print(f'  [{symbol}] bid={tick.bid_price:.2f} ask={tick.ask_price:.2f} spread={tick.spread:.6f}')

print('📡 启动 Binance WebSocket 连接...')
print('')

# 测试 1: BookTicker 模式
print('='*50)
print('测试 1: BookTicker 模式')
print('='*50)

manager = BinanceWSManager(
    symbols=['btcusdt', 'ethusdt'],
    stream_type='bookTicker',
    on_tick=on_tick,
)
manager.start()

print('等待 5 秒接收数据...')
time.sleep(5)

print('')
print('📊 当前数据:')
for sym in ['btcusdt', 'ethusdt']:
    tick = manager.get_latest(sym)
    if tick:
        print(f'  {sym.upper()}: bid={tick[\"bid_price\"]:.2f} ask={tick[\"ask_price\"]:.2f} mid={tick[\"mid_price\"]:.2f}')
    else:
        print(f'  {sym.upper()}: 暂无数据')

stats = manager.get_stats()
print('')
print(f'📈 统计: 收到 {stats[\"msg_count\"]} 条消息, 连接状态: {\"正常\" if stats[\"connected\"] else \"断开\"}')

manager.stop()
time.sleep(1)

# 测试 2: Ticker 模式
print('')
print('='*50)
print('测试 2: Ticker 模式（完整行情）')
print('='*50)

manager2 = BinanceWSManager(
    symbols=['btcusdt'],
    stream_type='ticker',
)
manager2.start()

time.sleep(3)
tick = manager2.get_latest('btcusdt')
if tick:
    print(f'  BTCUSDT:')
    print(f'    当前价: {tick[\"price\"]:.2f}')
    print(f'    24h涨跌: {tick[\"price_change\"]:+.2f} ({tick[\"price_change_pct\"]:+.2f}%)')
    print(f'    24h范围: {tick[\"low_24h\"]:.2f} - {tick[\"high_24h\"]:.2f}')
else:
    print('  暂无数据')

manager2.stop()
time.sleep(1)

# 测试 3: 便捷函数
print('')
print('='*50)
print('测试 3: 便捷函数')
print('='*50)

manager3 = BinanceWSManager(symbols=['btcusdt'], stream_type='bookTicker')
manager3.start()

print('等待获取价格...')
price = None
for i in range(30):
    price = get_binance_price('BTCUSDT')
    if price:
        break
    time.sleep(0.2)

if price:
    print(f'  BTCUSDT 价格: {price:.2f} USDT')
else:
    print('  获取价格失败')

manager3.stop()

print('')
print('✅ 测试完成!')
"
