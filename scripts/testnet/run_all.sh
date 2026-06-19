#!/usr/bin/env bash
# run_all.sh — execute every fake-amoy scenario and print a comparison report.
#
# Usage:
#   ./scripts/testnet/run_all.sh
#
# Each scenario produces its own timestamped run directory under main/runs/.
# The runner validates expected trigger_reason / filled_count and exits
# non-zero if any scenario diverges.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PY="${PROJECT_ROOT}/.venv/bin/python"

if [[ ! -x "$PY" ]]; then
  echo "❌ Python venv not found at $PY" >&2
  echo "   Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

cd "$PROJECT_ROOT"

echo "▶ Fake-amoy end-to-end test"
echo "  Project root: $PROJECT_ROOT"
echo

"${PY}" scripts/testnet/fake_amoy_runner.py all --run-dir main/runs

echo
echo "▶ Cross-scenario report"
"${PY}" scripts/testnet/compare_results.py --pattern "main/runs/fake_amoy_*/scenario_result.json"
