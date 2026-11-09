#!/usr/bin/env bash
# 平台运行在另一台机器（随身 WiFi 和 9eSIM 卡所在的主机）时，在本机使用：
#   scripts/remote.sh web       建 SSH 隧道、按需启动远端平台、在浏览器打开后台（窗口保持即隧道保持）
#   scripts/remote.sh status|start|stop|restart   在远端执行 scripts/platform.sh 对应命令
#
# 配置在 data/remote.env（不入库）：
#   ESIM_REMOTE=dual-5090          # ~/.ssh/config 里的主机名，需要免密钥登录
#   ESIM_REMOTE_DIR=esim-ble        # 远端项目目录（相对远端 HOME 或绝对路径）
#   ESIM_REMOTE_PORT=8787           # 远端平台端口
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
ENVF="$ROOT/data/remote.env"
[ -f "$ENVF" ] || { echo "没有 $ENVF，平台在本机运行：用 ./start.sh" >&2; exit 2; }
# shellcheck disable=SC1090
. "$ENVF"
R="${ESIM_REMOTE:?data/remote.env 里缺少 ESIM_REMOTE}"
RDIR="${ESIM_REMOTE_DIR:-esim-ble}"
RPORT="${ESIM_REMOTE_PORT:-8787}"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=10)

port_free() { ! lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

web() {
  local lport=$RPORT
  while ! port_free "$lport"; do lport=$((lport + 1)); done
  local out; out=$(mktemp -t esim-web)
  echo "连接 $R …"
  # stdin 保持打开：远端 hold 进程在隧道断开时才退出（平台据此判断“后台已关闭”）
  ( tail -f /dev/null ) | "${SSH[@]}" -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
      -L "$lport:127.0.0.1:$RPORT" "$R" "cd $RDIR && .venv/bin/esim-platform hold" >"$out" 2>&1 &
  local pid=$!
  local url="" i
  for i in $(seq 1 90); do
    url=$(sed -n 's/^URL //p' "$out" | head -1)
    [ -n "$url" ] && break
    if grep -q "^ERROR" "$out" || ! kill -0 "$pid" 2>/dev/null; then
      echo "失败："; cat "$out"; rm -f "$out"; return 1
    fi
    [ "$i" = 3 ] && echo "远端平台没在运行，正在启动（首次读卡约 10～20 秒）…"
    sleep 1
  done
  [ -n "$url" ] || { echo "90 秒内没有响应："; cat "$out"; kill "$pid" 2>/dev/null; return 1; }
  url=${url/127.0.0.1:$RPORT/127.0.0.1:$lport}
  echo "✓ 后台：$url"
  open "$url" 2>/dev/null || true
  echo
  echo "关闭这个窗口（或按 Ctrl+C）即断开隧道。"
  echo "如果平台是这次自动启动的，没有 AI 使用时约 2 分钟后会自动关闭。"
  trap 'kill $pid 2>/dev/null; rm -f "$out"; exit 0' INT TERM HUP
  wait "$pid"
  rm -f "$out"
}

case "${1:-}" in
  web) web ;;
  status|start|stop|restart) "${SSH[@]}" "$R" "cd $RDIR && scripts/platform.sh $1" ;;
  *) echo "用法：$0 web|status|start|stop|restart"; exit 2 ;;
esac
