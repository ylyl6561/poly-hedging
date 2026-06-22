#!/bin/bash
# 一键运行所有单元测试
# 用法: ./scripts/run_all_tests.sh

set -e

cd "$(dirname "$0")/.."

echo "=========================================="
echo "运行 poly-hedging 所有单元测试"
echo "=========================================="

source .venv/bin/activate

pytest tests/ -v --tb=short

echo ""
echo "=========================================="
echo "测试完成"
echo "=========================================="
