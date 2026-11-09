#!/usr/bin/env bash
# esim-ble 接码平台：启动 / 关闭 / 重启 / 状态
#   scripts/platform.sh start|stop|restart|status
# 也可以用项目根目录的 ./start.sh ./stop.sh ./restart.sh
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT" || exit 1
PY="$ROOT/.venv/bin/python"
PIDFILE="$ROOT/data/platform.pid"
LOG="$ROOT/logs/platform-stdout.log"
APPLOG="$ROOT/logs/platform.log"
MARKER="$ROOT/data/platform.autostart"   # written by the MCP server when it started the platform

c_ok=$'\033[32m'; c_err=$'\033[31m'; c_dim=$'\033[2m'; c_off=$'\033[0m'
ok()   { printf '%s✓%s %s\n' "$c_ok" "$c_off" "$*"; }
err()  { printf '%s✗%s %s\n' "$c_err" "$c_off" "$*" >&2; }
info() { printf '%s%s%s\n' "$c_dim" "$*" "$c_off"; }

port() {
  [ -x "$PY" ] && "$PY" -c 'import json,sys
try: print(json.load(open("data/platform.json")).get("port", 8787))
except Exception: print(8787)' 2>/dev/null || echo 8787
}

# PIDs of `esimble.platform serve` processes running from this checkout
pids() {
  local p cwd
  for p in $(pgrep -f "esimble.platform serve" 2>/dev/null); do
    cwd="$(lsof -a -p "$p" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)"
    [ -n "$cwd" ] && [ "$(cd "$cwd" 2>/dev/null && pwd -P)" = "$ROOT" ] && echo "$p"
  done
}

healthy() { curl -s --noproxy '*' -m 2 -o /dev/null "http://127.0.0.1:$(port)/"; }

setup() {
  if [ ! -x "$PY" ]; then
    command -v python3 >/dev/null || { err "需要 Python 3.10 以上（python3 不存在）"; exit 1; }
    info "首次运行：创建虚拟环境并安装依赖（约 1～2 分钟）…"
    python3 -m venv .venv || { err "创建虚拟环境失败"; exit 1; }
  fi
  if ! "$PY" -c "import fastapi, uvicorn, mcp, bleak" 2>/dev/null; then
    info "安装依赖…"
    "$PY" -m pip install -q --upgrade pip >/dev/null 2>&1
    "$PY" -m pip install -q -r requirements-platform.txt || { err "依赖安装失败"; exit 1; }
  fi
  # the MCP launcher (.venv/bin/esim-sms-mcp) must work from any directory
  if [ ! -x "$ROOT/.venv/bin/esim-sms-mcp" ] || ! (cd / && "$PY" -c "import esimble" 2>/dev/null); then
    info "安装 MCP 启动命令…"
    "$PY" -m pip install -q --no-deps -e "$ROOT" >/dev/null 2>&1 || { err "安装 esimble 失败"; exit 1; }
  fi
  mkdir -p data logs
}

login_url() { "$PY" -m esimble.platform token 2>&1 >/dev/null | tail -1; }

do_start() {
  local running; running="$(pids)"
  if [ -n "$running" ]; then
    ok "平台已在运行（PID $(echo $running)）"
    if [ -f "$MARKER" ]; then
      rm -f "$MARKER"; info "它原本由 MCP 自动启动，现在改为手动常驻，不会再被自动关闭"
    fi
    info "后台：$(login_url)"
    return 0
  fi
  if healthy; then
    err "端口 $(port) 已被其他程序占用，无法启动"; return 1
  fi
  setup
  info "启动中…"
  # new session, no controlling terminal: closing the Terminal window won't stop it
  nohup "$PY" -c 'import os, sys; os.setsid(); os.execv(sys.executable, [sys.executable, "-m", "esimble.platform", "serve"])' \
    >>"$LOG" 2>&1 </dev/null &
  local pid=$!
  echo "$pid" >"$PIDFILE"
  local i
  for i in $(seq 1 30); do
    if ! kill -0 "$pid" 2>/dev/null; then
      err "启动失败，最近的日志："; tail -15 "$LOG" >&2; rm -f "$PIDFILE"; return 1
    fi
    healthy && break
    sleep 1
  done
  if ! healthy; then err "30 秒内没有响应，查看日志：$LOG"; return 1; fi
  ok "平台已启动（PID ${pid}）"
  echo "  后台：$(login_url)"
  info "  日志：$APPLOG"
  info "  首次读卡约 10 秒；在后台“设备管理”里检查设备组状态"
}

do_stop() {
  local running; running="$(pids)"
  rm -f "$MARKER"
  if [ -z "$running" ]; then
    rm -f "$PIDFILE"; ok "平台没有在运行"; return 0
  fi
  if tail -5 "$APPLOG" 2>/dev/null | grep -qE "phase (switching|rebooting|waiting_network)"; then
    info "注意：平台可能正在切号，随身 WiFi 稍后会自行恢复"
  fi
  info "关闭中（PID $(echo $running)）…"
  kill $running 2>/dev/null
  local i
  for i in $(seq 1 15); do
    [ -z "$(pids)" ] && break
    sleep 1
  done
  running="$(pids)"
  if [ -n "$running" ]; then
    info "15 秒内未退出，强制结束"; kill -9 $running 2>/dev/null; sleep 1
  fi
  rm -f "$PIDFILE"
  [ -z "$(pids)" ] && ok "平台已关闭" || { err "关闭失败"; return 1; }
}

do_status() {
  local running; running="$(pids)"
  if [ -z "$running" ]; then echo "未运行"; return 3; fi
  ok "运行中（PID $(echo $running)，端口 $(port)）"
  if [ -f "$MARKER" ] && [ "$(cat "$MARKER")" = "$(echo $running)" ]; then
    echo "  由 MCP 自动启动：所有 MCP 客户端断开后会自动关闭（运行 ./start.sh 可改为常驻）"
  else
    echo "  手动启动：常驻运行"
  fi
  "$PY" - <<'EOF' 2>/dev/null
from esimble.platform.sdk import SmsPlatform
st = SmsPlatform().status()
for g in st.get("groups", []):
    cur = (g.get("current") or {})
    net = (g.get("modem") or {})
    print(f"  {g['name']}: {g['phase']}  在线号码 {cur.get('label') or '-'}  "
          f"{net.get('network_provider', '')} {net.get('network_type', '')}")
EOF
  echo "  后台：$(login_url)"
}

case "${1:-}" in
  start)   do_start ;;
  stop)    do_stop ;;
  restart) do_stop && do_start ;;
  status)  do_status ;;
  *) echo "用法：$0 start|stop|restart|status"; exit 2 ;;
esac
