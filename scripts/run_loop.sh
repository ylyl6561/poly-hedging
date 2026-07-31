#!/usr/bin/env bash
# 启动 smart_money 的常驻循环采集器（leaderboard/markets/trades/positions 多周期自调度）。
# 同时控制 dashboard (:8088)，stop / load / status 默认同时覆盖两者。
# 用法:
#   scripts/run_loop.sh                 # 默认周期 (trades/positions=5min, markets=6h, leaderboard=24h)
#   scripts/run_loop.sh stop            # 同时停 loop + dashboard
#   scripts/run_loop.sh status          # 同时看 loop + dashboard
#   scripts/run_loop.sh tail            # 实时跟踪 loop 日志
#   scripts/run_loop.sh restart         # 先 stop 再 start
#   scripts/run_loop.sh load            # 同时通过 launchd 注册 loop + dashboard
#   scripts/run_loop.sh unload          # 同时从 launchd 注销 loop + dashboard
#   scripts/run_loop.sh loop-stop       # 只停 loop
#   scripts/run_loop.sh dashboard-stop  # 只停 dashboard
#   scripts/run_loop.sh --loop-trades-seconds 60 --loop-positions-seconds 60 \
#                       --loop-follow-seconds 30   # 自定义周期
#
# 默认走 nohup 模式（适合临时调试）。推荐用 `load` 子命令持久化（不受 shell session 影响）。

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PY="${PROJECT_DIR}/.venv/bin/python"

# ---- 两个 service 的配置 ----
LOOP_PID_FILE="${PROJECT_DIR}/runs/smart_money_loop.pid"
LOOP_PLIST_DST="${HOME}/Library/LaunchAgents/com.polyhedging.smartmoney.plist"
LOOP_LABEL="com.polyhedging.smartmoney"
LOOP_LOG="${PROJECT_DIR}/logs/smart_money_loop.log"
LOOP_ERR="${PROJECT_DIR}/logs/smart_money_loop.err"

DASH_PID_FILE="${PROJECT_DIR}/runs/smart_money_dashboard.pid"
DASH_PLIST_DST="${HOME}/Library/LaunchAgents/com.polyhedging.smartmoney.dashboard.plist"
DASH_LABEL="com.polyhedging.smartmoney.dashboard"
DASH_LOG="${PROJECT_DIR}/logs/smart_money_dashboard.log"
DASH_ERR="${PROJECT_DIR}/logs/smart_money_dashboard.err"

mkdir -p "${PROJECT_DIR}/logs" "${PROJECT_DIR}/runs" "${HOME}/Library/LaunchAgents"

cmd="${1:-start}"
shift || true

# ---- 单个 service 的工具函数 ----

# launchctl 列出的进程 PID（load 模式下用）
launchctl_pid_of() {
    local lbl="$1"
    launchctl list 2>/dev/null | awk -v lbl="$lbl" '$3 == lbl {print $1; exit}'
}

is_running_service() {
    local svc="$1"
    local PID_FILE PLIST_DST LABEL
    case "$svc" in
        loop)      PID_FILE="$LOOP_PID_FILE"; PLIST_DST="$LOOP_PLIST_DST"; LABEL="$LOOP_LABEL" ;;
        dashboard) PID_FILE="$DASH_PID_FILE"; PLIST_DST="$DASH_PLIST_DST"; LABEL="$DASH_LABEL" ;;
        *) echo "❌ unknown service: $svc" >&2; return 2 ;;
    esac
    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid="$(cat "$PID_FILE" 2>/dev/null || true)"
        [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null && return 0
    fi
    local lpid
    lpid="$(launchctl_pid_of "$LABEL")"
    if [[ -n "$lpid" && "$lpid" != "-" ]]; then
        echo "$lpid" > "$PID_FILE"
        return 0
    fi
    return 1
}

stop_service() {
    local svc="$1"
    local PID_FILE PLIST_DST LABEL
    case "$svc" in
        loop)      PID_FILE="$LOOP_PID_FILE"; PLIST_DST="$LOOP_PLIST_DST"; LABEL="$LOOP_LABEL" ;;
        dashboard) PID_FILE="$DASH_PID_FILE"; PLIST_DST="$DASH_PLIST_DST"; LABEL="$DASH_LABEL" ;;
        *) echo "❌ unknown service: $svc" >&2; return 2 ;;
    esac
    if launchctl list 2>/dev/null | awk -v lbl="$LABEL" '$3 == lbl' | grep -q .; then
        echo "🛑 launchctl unload $LABEL"
        launchctl unload "$PLIST_DST" 2>/dev/null || true
    fi
    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid="$(cat "$PID_FILE")"
        if kill -0 "$pid" 2>/dev/null; then
            echo "🛑 发送 SIGTERM 给 ${svc} PID ${pid} ..."
            kill -TERM "$pid" 2>/dev/null || true
            for _ in $(seq 1 30); do
                kill -0 "$pid" 2>/dev/null || break
                sleep 1
            done
            kill -0 "$pid" 2>/dev/null && { echo "⚠️  30s 未退出，发送 SIGKILL"; kill -KILL "$pid" 2>/dev/null || true; }
            echo "✅ ${svc} 已停止"
        fi
        rm -f "$PID_FILE"
    fi
}

load_service() {
    local svc="$1"
    local plist_src="${SCRIPT_DIR}/com.polyhedging.smartmoney.${svc}.plist"
    local PLIST_DST LABEL
    case "$svc" in
        loop)      PLIST_DST="$LOOP_PLIST_DST"; LABEL="$LOOP_LABEL" ;;
        dashboard) PLIST_DST="$DASH_PLIST_DST"; LABEL="$DASH_LABEL" ;;
        *) echo "❌ unknown service: $svc" >&2; return 2 ;;
    esac
    if [[ ! -f "$plist_src" ]]; then
        echo "⚠️  ${svc}: 源 plist 不存在 ($plist_src)，跳过"
        return 0
    fi
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    cp -f "$plist_src" "$PLIST_DST"
    launchctl load -w "$PLIST_DST"
    echo "✅ launchd 已加载 $LABEL"
}

status_service() {
    local svc="$1"
    local PID_FILE PLIST_DST LABEL LOG_FILE ERR_FILE
    case "$svc" in
        loop)      PID_FILE="$LOOP_PID_FILE"; PLIST_DST="$LOOP_PLIST_DST"; LABEL="$LOOP_LABEL"; LOG_FILE="$LOOP_LOG"; ERR_FILE="$LOOP_ERR" ;;
        dashboard) PID_FILE="$DASH_PID_FILE"; PLIST_DST="$DASH_PLIST_DST"; LABEL="$DASH_LABEL"; LOG_FILE="$DASH_LOG"; ERR_FILE="$DASH_ERR" ;;
        *) echo "❌ unknown service: $svc" >&2; return 2 ;;
    esac
    if is_running_service "$svc"; then
        local pid
        pid="$(cat "$PID_FILE")"
        echo "✅ ${svc} 正在运行  PID=${pid}"
        if launchctl list 2>/dev/null | awk -v lbl="$LABEL" '$3 == lbl' | grep -q .; then
            echo "   (launchd 管理: ${LABEL})"
        fi
    else
        echo "❌ ${svc} 未运行"
    fi
    # loop 多 tail 几行 tick 日志；dashboard 不污染
    if [[ "$svc" == "loop" ]]; then
        if [[ -f "$ERR_FILE" ]]; then
            echo "---- 最近 tick ----"
            grep -h "loop tick" "$ERR_FILE" | tail -n 8 | sed -E 's/^[0-9-]+ [0-9:]+,\d+ \[INFO\] //'
        fi
        if [[ -f "$LOG_FILE" && -s "$LOG_FILE" ]]; then
            echo "---- 日志尾部 ($LOG_FILE) ----"
            tail -n 5 "$LOG_FILE"
        fi
    fi
}

unload_service() {
    local svc="$1"
    local PLIST_DST LABEL
    case "$svc" in
        loop)      PLIST_DST="$LOOP_PLIST_DST"; LABEL="$LOOP_LABEL" ;;
        dashboard) PLIST_DST="$DASH_PLIST_DST"; LABEL="$DASH_LABEL" ;;
        *) echo "❌ unknown service: $svc" >&2; return 2 ;;
    esac
    if launchctl list 2>/dev/null | awk -v lbl="$LABEL" '$3 == lbl' | grep -q .; then
        launchctl unload "$PLIST_DST" 2>/dev/null || true
        echo "✅ ${svc} 已从 launchd 注销"
    else
        echo "ℹ️  ${svc} 不在 launchd 中"
    fi
}

# ---- 顶层子命令 ----

case "$cmd" in
    stop)
        stop_service loop
        stop_service dashboard
        exit 0
        ;;
    loop-stop)
        stop_service loop
        exit 0
        ;;
    dashboard-stop)
        stop_service dashboard
        exit 0
        ;;
    status)
        set +e
        status_service loop
        echo
        status_service dashboard
        exit 0
        ;;
    loop-status)
        status_service loop
        exit 0
        ;;
    dashboard-status)
        status_service dashboard
        exit 0
        ;;
    tail)
        exec tail -F "$LOOP_LOG"
        ;;
    restart)
        stop_service loop
        stop_service dashboard
        ;;
    load)
        # 先保证两个都停（避免端口冲突等）
        stop_service loop || true
        stop_service dashboard || true
        # 注册 launchd
        load_service loop
        load_service dashboard
        sleep 3
        echo
        status_service loop
        echo
        status_service dashboard
        exit 0
        ;;
    loop-load)
        stop_service loop || true
        load_service loop
        sleep 3
        status_service loop
        exit 0
        ;;
    dashboard-load)
        stop_service dashboard || true
        load_service dashboard
        sleep 3
        status_service dashboard
        exit 0
        ;;
    unload)
        unload_service loop
        unload_service dashboard
        exit 0
        ;;
    start)
        is_running_service loop && { echo "✅ loop 已在运行 (PID=$(cat "$LOOP_PID_FILE"))"; exit 0; }
        is_running_service dashboard && echo "✅ dashboard 已在运行"
        ;;
    *)       # treat unknown subcmd as flags; fall through to start
             ;;
esac

# ---- 后面是 nohup 启动 loop 的细节（默认行为，向后兼容）----

if [[ ! -x "$PY" ]]; then
    echo "❌ 未找到虚拟环境 Python: $PY"
    exit 1
fi

if [[ -f "${PROJECT_DIR}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${PROJECT_DIR}/.env"
    set +a
fi

# 默认周期（可被环境变量覆盖，与原 scheduler/index.js 一致）
LEADERBOARD_SECONDS="${SM_LOOP_LEADERBOARD_SECONDS:-86400}"
MARKETS_SECONDS="${SM_LOOP_MARKETS_SECONDS:-21600}"
TRADES_SECONDS="${SM_LOOP_TRADES_SECONDS:-300}"
POSITIONS_SECONDS="${SM_LOOP_POSITIONS_SECONDS:-300}"
FOLLOW_SECONDS="${SM_LOOP_FOLLOW_SECONDS:-30}"
TICK_SECONDS="${SM_LOOP_TICK_SECONDS:-10}"

echo "🚀 启动 smart_money loop (nohup 模式)"
echo "  leaderboard: ${LEADERBOARD_SECONDS}s"
echo "  markets:     ${MARKETS_SECONDS}s"
echo "  trades:      ${TRADES_SECONDS}s"
echo "  positions:   ${POSITIONS_SECONDS}s"
echo "  follow:      ${FOLLOW_SECONDS}s   ← 跟单名单刷新 + 信号 + 跟单执行"
echo "  tick:        ${TICK_SECONDS}s"
echo "  日志:        ${PROJECT_DIR}/logs/smart_money_loop.log"
echo "  ⚠️  想要登录即启动、不受 shell session 影响，请使用 '$0 load'"
echo "  ℹ️  默认用 '$0 status' 可同时看 dashboard 状态"

cd "$PROJECT_DIR"
: > "${PROJECT_DIR}/logs/smart_money_loop.log"
: > "${PROJECT_DIR}/logs/smart_money_loop.err"

# 双重 fork 让进程彻底脱离 shell session
(
    (
        nohup "$PY" -m smart_money run --job all --loop \
            --loop-leaderboard-seconds "$LEADERBOARD_SECONDS" \
            --loop-markets-seconds    "$MARKETS_SECONDS" \
            --loop-trades-seconds     "$TRADES_SECONDS" \
            --loop-positions-seconds  "$POSITIONS_SECONDS" \
            --loop-tick-seconds       "$TICK_SECONDS" \
            --loop-follow-seconds     "$FOLLOW_SECONDS" \
            "$@" \
            >> "${PROJECT_DIR}/logs/smart_money_loop.log" 2>> "${PROJECT_DIR}/logs/smart_money_loop.err" < /dev/null &
        echo $! > "${PROJECT_DIR}/runs/smart_money_loop.pid"
    ) &
)

# 等待 3 秒确认进程存活
sleep 3
if kill -0 "$(cat "${PROJECT_DIR}/runs/smart_money_loop.pid")" 2>/dev/null; then
    echo "✅ loop 已启动  PID=$(cat "${PROJECT_DIR}/runs/smart_money_loop.pid")"
    echo "  查看日志: $0 tail"
    echo "  停止:     $0 stop    # 同时停 dashboard"
    echo "  持久化:   $0 load    # 推荐，loop+dashboard 都注册"
else
    echo "❌ loop 启动失败，请检查 ${PROJECT_DIR}/logs/smart_money_loop.log"
    rm -f "${PROJECT_DIR}/runs/smart_money_loop.pid"
    exit 1
fi
