#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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

# Defaults align with fastloop_trader.py CLI
LOOP_INTERVAL="${SIMMER_FASTLOOP_RUN_LOOP_INTERVAL:-${SIMMER_FASTLOOP_LOOP_INTERVAL:-10}}"
OPEN_DELAY="${SIMMER_FASTLOOP_RUN_OPEN_DELAY:-2}"

if [[ -z "${SIMMER_API_KEY:-}" ]]; then
  echo "❌ 缺少 SIMMER_API_KEY，请先写入 ${SCRIPT_DIR}/.env"
  exit 1
fi

echo "🧪 准备启动 Simmer FastLoop Dry Run"
echo "  模式: dry-run（不真实下单）"
echo "  调度: --scheduled-loop"
echo "  检查间隔: ${LOOP_INTERVAL}s"
echo "  开盘采样延迟: ${OPEN_DELAY}s"
echo "  运行命令: .venv/bin/python main/fastloop_trader.py --dry-run --scheduled-loop --loop-interval ${LOOP_INTERVAL} --open-delay ${OPEN_DELAY}"
echo

auto_quiet="${SIMMER_FASTLOOP_QUIET:-1}"
QUIET_ARGS=()
if [[ "${auto_quiet}" == "1" ]]; then
  QUIET_ARGS=("--quiet")
fi

cd "$SCRIPT_DIR"
exec "$PY" main/fastloop_trader.py \
  --dry-run \
  --scheduled-loop \
  --loop-interval "$LOOP_INTERVAL" \
  --open-delay "$OPEN_DELAY" \
  "${QUIET_ARGS[@]}" \
  "$@"
