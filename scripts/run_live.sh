#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${SCRIPT_DIR}/.venv/bin/python"

if [[ ! -x "$PY" ]]; then
  echo "❌ 未找到虚拟环境 Python: $PY"
  echo "请先创建 .venv 并安装 requirements.txt"
  exit 1
fi

if [[ -f "${SCRIPT_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${SCRIPT_DIR}/.env"
  set +a
fi

export SIMMER_FASTLOOP_STRATEGY_MODE="${SIMMER_FASTLOOP_STRATEGY_MODE:-oracle_latency}"
export SIMMER_FASTLOOP_EXECUTION_ROUTE="${SIMMER_FASTLOOP_EXECUTION_ROUTE:-direct_clob}"
export SIMMER_FASTLOOP_ORDER_TYPE="${SIMMER_FASTLOOP_ORDER_TYPE:-FAK}"

# Defaults align with fastloop_trader.py CLI
LOOP_INTERVAL="${SIMMER_FASTLOOP_RUN_LOOP_INTERVAL:-${SIMMER_FASTLOOP_LOOP_INTERVAL:-10}}"
OPEN_DELAY="${SIMMER_FASTLOOP_RUN_OPEN_DELAY:-2}"

if [[ -z "${SIMMER_API_KEY:-}" ]]; then
  echo "❌ 缺少 SIMMER_API_KEY，请先写入 ${SCRIPT_DIR}/.env"
  exit 1
fi

if [[ "${SIMMER_FASTLOOP_EXECUTION_ROUTE}" == "direct_clob" && -z "${WALLET_PRIVATE_KEY:-}" ]]; then
  echo "❌ direct_clob 实盘需要 WALLET_PRIVATE_KEY，请先写入 ${SCRIPT_DIR}/.env"
  exit 1
fi

echo "🚀 准备启动 Simmer FastLoop 实盘"
echo "  策略模式: ${SIMMER_FASTLOOP_STRATEGY_MODE}"
echo "  下单路由: ${SIMMER_FASTLOOP_EXECUTION_ROUTE}"
echo "  订单类型: ${SIMMER_FASTLOOP_ORDER_TYPE}"
echo "  检查间隔: ${LOOP_INTERVAL}s"
echo "  开盘采样延迟: ${OPEN_DELAY}s"
echo "  运行命令: .venv/bin/python main/fastloop_trader.py --live --scheduled-loop --loop-interval ${LOOP_INTERVAL} --open-delay ${OPEN_DELAY}"
echo
echo "⚠️  这是真实下单模式，会使用你的配置和钱包资金。3 秒后启动，按 Ctrl-C 可取消。"
sleep "${RUN_LIVE_COUNTDOWN_SECONDS:-3}"

cd "$SCRIPT_DIR"
exec "$PY" main/fastloop_trader.py \
  --live \
  --scheduled-loop \
  --loop-interval "$LOOP_INTERVAL" \
  --open-delay "$OPEN_DELAY" \
  "$@"
