"""esim-ble command line.

  esimble config [--ble-device NAME] [--mifi-host IP] [--mifi-password]
  esimble card list|add|delete|enable|disable|nickname ...
  esimble mifi status|inbox|send|sms-ready|wifi|reboot|at ...
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import sys

from . import config as config_mod
from .card_service import CardService
from .config import DATA_DIR, LOG_DIR
from .db import Database
from .mifi import Mifi


def _decode_qr(path: str) -> str:
    try:
        import cv2
    except ImportError:
        raise SystemExit("QR decoding needs opencv-python-headless")
    img = cv2.imread(path)
    if img is None:
        raise SystemExit(f"cannot read image {path}")
    text = cv2.QRCodeDetector().detectAndDecode(img)[0]
    if not text:
        raise SystemExit(f"no QR code found in {path}")
    return text


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _print_profiles(rows: list[dict]) -> None:
    for p in rows:
        mark = "*" if p["state"] == "enabled" else " "
        nick = f"  [{p['nickname']}]" if p.get("nickname") else ""
        print(f" {mark} {p['iccid']:<22} {p['state']:<9} {p['provider']} / {p['name']}{nick}")


async def card_cmd(args, cfg, db) -> int:
    svc = CardService(cfg, db)
    if args.action == "list":
        info = await svc.refresh()
        print("EID:", info["eid"])
        if info["free_nvm"] is not None:
            print(f"free NVM: {info['free_nvm'] / 1024:.2f} KB")
        _print_profiles(info["profiles"])
    elif args.action == "add":
        code = _decode_qr(args.qr) if args.qr else args.code
        if not code:
            raise SystemExit("give an activation code or --qr image")
        r = await svc.add(code, args.nickname, args.cc)
        print("installed", r["iccid"])
        _print_profiles((await svc.refresh())["profiles"])
    elif args.action == "delete":
        if not args.yes and input(f"Delete {args.iccid} from the card? This cannot be undone [y/N] ") != "y":
            return 1
        await svc.delete(args.iccid)
        print("deleted", args.iccid)
    elif args.action == "enable":
        print("enable result", await svc.enable(args.iccid))
    elif args.action == "disable":
        print("disable result", await svc.disable(args.iccid))
    elif args.action == "nickname":
        await svc.set_nickname(args.iccid, args.nickname)
        print("nickname set")
    return 0


async def mifi_cmd(args, cfg) -> int:
    m = Mifi(cfg.mifi_host, cfg.mifi_password)
    if args.action == "status":
        _print(await m.status())
    elif args.action == "inbox":
        msgs = await m.inbox(args.store)
        if args.direction != "all":
            msgs = [x for x in msgs if x["direction"] == args.direction]
        _print(msgs[: args.limit])
    elif args.action == "send":
        _print(await m.send(args.number, args.text))
    elif args.action == "sms-ready":
        state = await m.sms_ready()
        print({"3": "ready", "2": "failed (reboot the MiFi)"}.get(state, f"initialising ({state})"))
    elif args.action == "wifi":
        if args.state != "status":
            await m.set_wifi(args.state == "on")
        _print(await m.wifi_state())
    elif args.action == "reboot":
        if not args.yes and input("Reboot the MiFi now? [y/N] ") != "y":
            return 1
        _print(await m.reboot())
        print("rebooting; check again with `mifi status` / `mifi sms-ready` in 1-5 minutes")
    elif args.action == "at":
        print(await m.at(args.command))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="esimble", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="group", required=True)

    c = sub.add_parser("config", help="show / change local settings")
    c.add_argument("--ble-device")
    c.add_argument("--ble-password", action="store_true", help="prompt for the card password")
    c.add_argument("--mifi-host")
    c.add_argument("--mifi-password", action="store_true", help="prompt for the MiFi admin password")
    c.add_argument("--secure-tls", action="store_true", help="verify SM-DP+ TLS with system roots")

    card = sub.add_parser("card", help="manage profiles on the 9eSIM card over Bluetooth")
    cs = card.add_subparsers(dest="action", required=True)
    cs.add_parser("list")
    a = cs.add_parser("add")
    a.add_argument("code", nargs="?", help="LPA:1$smdp$matchingId")
    a.add_argument("--qr")
    a.add_argument("--cc", help="confirmation code")
    a.add_argument("--nickname")
    d = cs.add_parser("delete")
    d.add_argument("iccid")
    d.add_argument("-y", "--yes", action="store_true")
    for name in ("enable", "disable"):
        cs.add_parser(name).add_argument("iccid")
    n = cs.add_parser("nickname")
    n.add_argument("iccid")
    n.add_argument("nickname")

    mifi = sub.add_parser("mifi", help="MiFi modem status and SMS")
    ms = mifi.add_subparsers(dest="action", required=True)
    ms.add_parser("status")
    i = ms.add_parser("inbox")
    i.add_argument("--store", type=int, choices=[0, 1], default=1, help="1 = device, 0 = SIM")
    i.add_argument("--direction", choices=["in", "out", "all"], default="all")
    i.add_argument("--limit", type=int, default=50)
    s = ms.add_parser("send")
    s.add_argument("number")
    s.add_argument("text")
    ms.add_parser("sms-ready", help="SMS subsystem init state")
    w = ms.add_parser("wifi", help="Wi-Fi hotspot on/off")
    w.add_argument("state", choices=["on", "off", "status"])
    r = ms.add_parser("reboot", help="reboot the MiFi (re-initialises the SMS subsystem)")
    r.add_argument("-y", "--yes", action="store_true")
    a2 = ms.add_parser("at", help="run an AT command through the MiFi")
    a2.add_argument("command")

    args = ap.parse_args(argv)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S",
        handlers=[logging.FileHandler(LOG_DIR / "esimble.log"), logging.StreamHandler()])
    for noisy in ("bleak", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.DEBUG if args.verbose else logging.WARNING)

    cfg = config_mod.load()
    if args.group == "config":
        if args.ble_device:
            cfg.ble_device = args.ble_device
        if args.ble_password:
            cfg.ble_password = getpass.getpass("9eSIM password (empty = device default): ") or None
        if args.mifi_host:
            cfg.mifi_host = args.mifi_host
        if args.mifi_password:
            cfg.mifi_password = getpass.getpass("MiFi admin password: ")
        if args.secure_tls:
            cfg.insecure_tls = False
        cfg.save()
        _print({k: cfg.public()[k] for k in ("ble_device", "mifi_host", "mifi_password_set",
                                              "insecure_tls")})
        return 0

    db = Database(DATA_DIR / "esimble.db")
    if args.group == "card":
        return asyncio.run(card_cmd(args, cfg, db))
    return asyncio.run(mifi_cmd(args, cfg))


if __name__ == "__main__":
    sys.exit(main())
