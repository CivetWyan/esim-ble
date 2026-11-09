"""Device groups (one 9eSIM card + one MiFi each), discovery and onboarding helpers."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, fields

from ..card.nble import default_password_for_device_name
from ..card_service import CardService
from ..config import Config
from ..mifi import MifiError
from .modem import PlatformMifi
from .store import PlatformDB, now_iso

log = logging.getLogger("devices")

DEFAULT_MIFI_HOST = "192.168.0.1"
DEFAULT_MIFI_PASSWORD = "admin"


@dataclass
class GroupConfig:
    id: str
    name: str = ""
    enabled: bool = True
    ble_device: str = ""
    ble_password: str | None = None      # None = derived default
    mifi_host: str = DEFAULT_MIFI_HOST
    mifi_password: str = DEFAULT_MIFI_PASSWORD
    mifi_imei: str = ""
    home_iccid: str = ""
    rotation_enabled: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "GroupConfig":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})

    def to_dict(self) -> dict:
        return asdict(self)

    def public(self) -> dict:
        d = asdict(self)
        d["ble_password_set"] = self.ble_password is not None
        d["mifi_password_set"] = bool(self.mifi_password)
        d.pop("ble_password")
        d.pop("mifi_password")
        return d

    def card_config(self, insecure_tls: bool) -> Config:
        return Config(ble_device=self.ble_device, ble_password=self.ble_password,
                      insecure_tls=insecure_tls, mifi_host=self.mifi_host,
                      mifi_password=self.mifi_password)


class GroupView:
    """Engine settings for one group: per-group fields come from the group, the rest global."""
    PER_GROUP = ("home_iccid", "rotation_enabled")

    def __init__(self, settings, group: GroupConfig):
        self.__dict__["_s"], self.__dict__["_g"] = settings, group

    def __getattr__(self, k):
        return getattr(self._g if k in self.PER_GROUP else self._s, k)

    def __setattr__(self, k, v):
        setattr(self._g if k in self.PER_GROUP else self._s, k, v)


class GroupDB:
    """PlatformDB scoped to one group's card: profile listing and card sync only see
    this group's profiles; everything else is shared."""

    def __init__(self, db: PlatformDB, group: GroupConfig):
        self._db, self._g = db, group

    def __getattr__(self, k):
        return getattr(self._db, k)

    def profiles(self, include_removed: bool = False) -> list[dict]:
        sql = "SELECT * FROM profiles WHERE device_id=?" + ("" if include_removed else " AND on_card=1")
        return self._db.q(sql + " ORDER BY priority DESC, created_at", (self._g.id,))

    def event(self, kind: str, message: str, level: str = "info") -> None:
        self._db.event(kind, f"[{self._g.name or self._g.id}] {message}", level)

    def sync_card_profiles(self, card_profiles: list) -> None:
        db, gid, ts, seen = self._db, self._g.id, now_iso(), set()
        for p in card_profiles:
            seen.add(p.iccid)
            if db.one("SELECT 1 FROM profiles WHERE iccid=?", (p.iccid,)):
                db.x("UPDATE profiles SET card_provider=?, card_name=?, card_nickname=?, card_state=?, "
                     "profile_class=?, on_card=1, device_id=?, updated_at=? WHERE iccid=?",
                     (p.provider, p.name, p.nickname, p.state, p.profile_class, gid, ts, p.iccid))
            else:
                db.x("INSERT INTO profiles(iccid, label, card_provider, card_name, card_nickname, "
                     "card_state, profile_class, device_id, created_at, updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (p.iccid, p.nickname or p.name, p.provider, p.name, p.nickname, p.state,
                      p.profile_class, gid, ts, ts))
        for row in db.q("SELECT iccid FROM profiles WHERE on_card=1 AND device_id=?", (gid,)):
            if row["iccid"] not in seen:
                db.x("UPDATE profiles SET on_card=0, card_state='removed', updated_at=? WHERE iccid=?",
                     (ts, row["iccid"]))


# ------------------------------------------------------------------ cards (BLE)
async def scan_cards(timeout: float = 6.0) -> list[dict]:
    from bleak import BleakScanner
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    out = []
    for addr, (dev, adv) in found.items():
        name = dev.name or adv.local_name or ""
        if name.upper().startswith("9ESIM"):
            out.append({"name": name, "address": addr, "rssi": adv.rssi,
                        "default_password": default_password_for_device_name(name)})
    return sorted(out, key=lambda d: -(d["rssi"] or -999))


async def probe_card(name: str, password: str | None, lock: asyncio.Lock,
                     insecure_tls: bool = True) -> dict:
    svc = CardService(Config(ble_device=name, ble_password=password or None,
                             insecure_tls=insecure_tls), None)
    svc.lock = lock
    t0 = time.monotonic()
    async with svc.session("probe card") as card:
        eid = await card.get_eid()
        free = await card.free_nvm()
        profiles = await card.list_profiles()
    return {"name": name, "eid": eid, "free_nvm": free, "seconds": round(time.monotonic() - t0, 1),
            "profiles": [{"iccid": p.iccid, "state": p.state, "provider": p.provider,
                          "name": p.name, "nickname": p.nickname} for p in profiles]}


# ------------------------------------------------------------------ MiFis (USB)
def _run(*cmd: str) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _linux_interfaces() -> list[dict]:
    import json as _json
    try:
        addrs = _json.loads(_run("ip", "-j", "-4", "addr") or "[]")
        routes = _json.loads(_run("ip", "-j", "-4", "route") or "[]")
    except ValueError:
        return []
    gw = {r.get("dev"): r.get("gateway") for r in routes if r.get("gateway")}
    out = []
    for a in addrs:
        for info in a.get("addr_info", []):
            ip = info.get("local", "")
            if not ip or ip.startswith("127."):
                continue
            net = ipaddress.ip_network(f"{ip}/{info.get('prefixlen', 24)}", strict=False)
            out.append({"iface": a.get("ifname", ""), "ip": ip, "netmask": str(net.netmask),
                        "subnet": str(net), "router": gw.get(a.get("ifname"), "") or ""})
    return out


def local_interfaces() -> list[dict]:
    import sys as _sys
    if _sys.platform.startswith("linux"):
        return _linux_interfaces()
    out, cur = [], None
    for line in _run("ifconfig").splitlines():
        if line and not line[0].isspace():
            cur = line.split(":")[0]
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-fA-F]+)", line)
        if m and cur and not m.group(1).startswith("127."):
            mask = socket.inet_ntoa(int(m.group(2), 16).to_bytes(4, "big"))
            net = ipaddress.ip_network(f"{m.group(1)}/{mask}", strict=False)
            out.append({"iface": cur, "ip": m.group(1), "netmask": mask, "subnet": str(net),
                        "router": _run("ipconfig", "getoption", cur, "router").strip()})
    return out


def probe_mifi_public(host: str) -> dict | None:
    """Unauthenticated probe: is there a ZTE-goform MiFi at host?"""
    m = PlatformMifi(host, "")
    try:
        r = m._request({"cmd": "modem_main_state,network_type,network_provider,signalbar,loginfo",
                        "multi_data": 1}, timeout=2)
    except (MifiError, ValueError):
        return None
    return r if isinstance(r, dict) and "modem_main_state" in r else None


def probe_mifi(host: str, password: str) -> dict:
    """Log in and read identity + state. Raises MifiError."""
    m = PlatformMifi(host, password)
    m._login()
    r = m._authed({"cmd": "imei,hardware_version,wa_inner_version,lan_ipaddr,lan_netmask,"
                          "network_type,network_provider,signalbar,sim_current_type,ziccid,msisdn,"
                          "modem_main_state", "multi_data": 1})
    return {"host": host, **{k: r.get(k, "") for k in (
        "imei", "hardware_version", "wa_inner_version", "lan_ipaddr", "lan_netmask", "network_type",
        "network_provider", "signalbar", "sim_current_type", "ziccid", "msisdn", "modem_main_state")}}


def scan_mifis(groups: list[GroupConfig]) -> dict:
    ifaces = local_interfaces()
    by_subnet: dict[str, list[dict]] = {}
    for i in ifaces:
        by_subnet.setdefault(i["subnet"], []).append(i)
    hosts = {i["router"] or str(next(ipaddress.ip_network(i["subnet"]).hosts())) for i in ifaces}
    hosts.add(DEFAULT_MIFI_HOST)
    hosts |= {g.mifi_host for g in groups}
    with ThreadPoolExecutor(8) as ex:
        probes = dict(zip(hosts, ex.map(probe_mifi_public, hosts)))
    found = []
    for host, info in probes.items():
        if not info:
            continue
        ifs = [i for i in ifaces if ipaddress.ip_address(host) in ipaddress.ip_network(i["subnet"])]
        g = next((g for g in groups if g.mifi_host == host), None)
        found.append({"host": host, "interfaces": [i["iface"] for i in ifs],
                      "network_type": info.get("network_type", ""),
                      "network_provider": info.get("network_provider", ""),
                      "signalbar": info.get("signalbar", ""),
                      "group_id": g.id if g else None, "group_name": g.name if g else None,
                      "conflict": len(ifs) > 1})
    conflicts = [{"subnet": s, "interfaces": [i["iface"] for i in v]}
                 for s, v in by_subnet.items() if len(v) > 1]
    used = {ipaddress.ip_network(i["subnet"]) for i in ifaces}
    return {"mifis": sorted(found, key=lambda d: d["host"]), "conflicts": conflicts,
            "interfaces": ifaces, "suggested_host": suggest_host(used, groups)}


def suggest_host(used_subnets: set, groups: list[GroupConfig]) -> str:
    taken = set(used_subnets) | {ipaddress.ip_network(f"{g.mifi_host}/24", strict=False)
                                 for g in groups if g.mifi_host}
    for n in range(1, 250):
        net = ipaddress.ip_network(f"192.168.{n}.0/24")
        if net not in taken:
            return f"192.168.{n}.1"
    return "192.168.250.1"


def relocate_mifi(host: str, password: str, new_host: str, timeout: float = 240) -> dict:
    """Change a MiFi's LAN address (it reboots) and wait for it at the new address."""
    new = ipaddress.ip_address(new_host)
    if not new.is_private or new.version != 4 or not new_host.endswith(".1"):
        raise ValueError("new address must be a private IPv4 ending in .1, e.g. 192.168.1.1")
    m = PlatformMifi(host, password)
    before = probe_mifi(host, password)
    prefix = new_host.rsplit(".", 1)[0]
    cur = m._authed({"cmd": "dhcpLease_hour", "multi_data": 1})
    try:
        r = m._authed({"goformId": "DHCP_SETTING", "lanIp": new_host, "lanNetmask": "255.255.255.0",
                       "lanDhcpType": "SERVER", "dhcpStart": f"{prefix}.100",
                       "dhcpEnd": f"{prefix}.200", "dhcpLease": cur.get("dhcpLease_hour") or "24",
                       "dhcp_reboot_flag": 1}, True)
        log.info("DHCP_SETTING on %s -> %s: %s", host, new_host, r)
        if r.get("result") not in ("success", None):
            raise MifiError(f"MiFi rejected the new address: {r}")
    except MifiError as e:
        if "unreachable" not in str(e):
            raise
        log.info("DHCP_SETTING request ended with %s (device rebooting)", e)
    deadline = time.monotonic() + timeout
    m2 = PlatformMifi(new_host, password)
    while time.monotonic() < deadline:
        time.sleep(4)
        if m2.reachable_sync():
            after = probe_mifi(new_host, password)
            if before["imei"] and after["imei"] != before["imei"]:
                raise MifiError(f"a different device answered at {new_host}")
            return {"host": new_host, "imei": after["imei"],
                    "seconds": round(timeout - (deadline - time.monotonic()))}
    raise MifiError(f"MiFi did not come back at {new_host} within {int(timeout)} s; "
                    "unplug and replug its USB cable, then scan again")
