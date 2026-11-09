#!/usr/bin/env bash
# Linux（NetworkManager）：让 USB 随身 WiFi 的网卡自动获取地址，但不抢默认路由。
#
#   sudo scripts/linux/setup-mifi-network.sh            # 自动找 ZTE/SZXF 随身 WiFi 网卡
#   sudo scripts/linux/setup-mifi-network.sh enx...     # 或者指定网卡名
#
# 为每块网卡建一个 NetworkManager 连接 "esim-mifi-<网卡>"：
#   - IPv4 用 DHCP，但 never-default：不添加默认路由、不改 DNS，主机的上网线路不变
#     （否则所有流量会改走随身 WiFi 的 4G，公网端口转发 / SSH 也可能断）
#   - 关闭 IPv6
#   - 自动连接、无限重试：平台每次切号会重启随身 WiFi，网卡掉线重连后自动恢复
# 可以重复运行；删除：sudo nmcli connection delete esim-mifi-<网卡>
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "请用 sudo 运行：sudo $0 $*" >&2; exit 1; }
command -v nmcli >/dev/null || { echo "需要 NetworkManager（nmcli）" >&2; exit 1; }

find_mifis() {
  local n dev v
  for n in /sys/class/net/*; do
    n=${n##*/}
    dev=$(readlink -f "/sys/class/net/$n/device" 2>/dev/null) || continue
    # walk up to the USB device node and read its vendor id
    while [ -n "$dev" ] && [ "$dev" != "/" ]; do
      if [ -f "$dev/idVendor" ]; then
        v=$(cat "$dev/idVendor")
        [ "$v" = "19d2" ] && echo "$n"     # 19d2 = ZTE (SZXF / MF761W 等 ZTE 方案随身 WiFi)
        break
      fi
      dev=${dev%/*}
    done
  done
}

if [ $# -gt 0 ]; then IFACES="$*"; else IFACES=$(find_mifis); fi
[ -n "$IFACES" ] || { echo "没有找到 ZTE 随身 WiFi 网卡；插好 USB 后再试，或手动指定网卡名" >&2; exit 1; }

before=$(ip -4 route show default)
for IF in $IFACES; do
  [ -e "/sys/class/net/$IF" ] || { echo "网卡 $IF 不存在" >&2; exit 1; }
  NAME="esim-mifi-$IF"
  nmcli -t -f NAME connection show | grep -qx "$NAME" && nmcli connection delete "$NAME" >/dev/null
  nmcli connection add type ethernet ifname "$IF" con-name "$NAME" \
    ipv4.method auto ipv4.never-default yes ipv4.ignore-auto-dns yes ipv4.route-metric 900 \
    ipv6.method disabled \
    connection.autoconnect yes connection.autoconnect-retries 0 connection.autoconnect-priority 10 \
    >/dev/null
  nmcli connection up "$NAME" >/dev/null || true
  for _ in $(seq 1 20); do
    ip -4 -br addr show "$IF" | grep -q "inet\| [0-9]" && break
    sleep 1
  done
  echo "$IF: $(ip -4 -br addr show "$IF" | awk '{print $3}')"
done
after=$(ip -4 route show default)
echo "默认路由（未改变）：$after"
[ "$before" = "$after" ] || echo "警告：默认路由发生了变化，请检查！" >&2
