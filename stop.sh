#!/usr/bin/env bash
# 一键关闭 esim-ble 接码平台
exec "$(cd "$(dirname "$0")" && pwd)/scripts/platform.sh" stop "$@"
