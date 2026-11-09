#!/usr/bin/env bash
# 双击运行：状态 esim-ble 接码平台（macOS 会在“终端”里执行）
ROOT="$(cd "$(dirname "$0")" && pwd -P)"
cd "$ROOT" || exit 1
[ -t 1 ] && printf '\033]0;esim-ble · 状态\007'
echo "== esim-ble 接码平台 · 状态 =="
"$ROOT/scripts/platform.sh" status
rc=$?
if [ -t 1 ]; then
  echo
  read -r -n 1 -s -p "按任意键关闭窗口…"
  echo
fi
exit $rc
