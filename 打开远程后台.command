#!/usr/bin/env bash
# 双击运行：平台在另一台主机上时，建 SSH 隧道并在浏览器打开后台（配置见 data/remote.env）
ROOT="$(cd "$(dirname "$0")" && pwd -P)"
[ -t 1 ] && printf '\033]0;esim-ble · 远程后台\007'
echo "== esim-ble 接码平台 · 远程后台 =="
"$ROOT/scripts/remote.sh" web
rc=$?
if [ -t 1 ] && [ $rc -ne 0 ]; then echo; read -r -n 1 -s -p "按任意键关闭窗口…"; echo; fi
exit $rc
