#!/usr/bin/env bash
# 双击运行：重启 esim-ble 接码平台（macOS 会在“终端”里执行）
ROOT="$(cd "$(dirname "$0")" && pwd -P)"
cd "$ROOT" || exit 1
[ -t 1 ] && printf '\033]0;esim-ble · 重启\007'
echo "== esim-ble 接码平台 · 重启 =="
"$ROOT/scripts/platform.sh" restart
rc=$?
if [ -t 1 ]; then
  if [ $rc -eq 0 ] && [ -x "$ROOT/.venv/bin/python" ]; then
    url="$("$ROOT/.venv/bin/python" -m esimble.platform token 2>&1 >/dev/null | tail -1)"
    case "$url" in http://127.0.0.1:*) echo "正在浏览器中打开后台…"; open "$url" ;; esac
  fi
  echo
  read -r -n 1 -s -p "按任意键关闭窗口…"
  echo
fi
exit $rc
