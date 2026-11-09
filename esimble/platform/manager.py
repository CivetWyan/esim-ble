"""Runs one Engine per device group and routes numbers / holds to the right one."""

from __future__ import annotations

import asyncio
import logging
import secrets

from ..card_service import CardService
from ..config import Config
from .devices import GroupConfig, GroupDB, GroupView
from .engine import Engine, PlatformError
from .modem import PlatformMifi
from .settings import PlatformSettings
from .store import PlatformDB

log = logging.getLogger("manager")


class Manager:
    def __init__(self, cfg: Config, settings: PlatformSettings, db: PlatformDB,
                 engines: dict[str, Engine] | None = None):
        self.cfg, self.s, self.db = cfg, settings, db
        self.ble_lock = asyncio.Lock()          # one BLE operation at a time, across groups
        self.engines: dict[str, Engine] = {}
        self._started = False
        if engines is not None:                 # tests / single-engine use
            self.engines = engines
            self.groups = [e.group or GroupConfig(id=gid, name="设备组 1",
                                                  ble_device=e.cfg.ble_device,
                                                  mifi_host=e.cfg.mifi_host)
                           for gid, e in engines.items()]
            return
        self.groups = [GroupConfig.from_dict(d) for d in settings.groups]
        if not self.groups:                     # migrate the single-device setup
            self.groups = [GroupConfig(
                id="g1", name="设备组 1", ble_device=cfg.ble_device, ble_password=cfg.ble_password,
                mifi_host=cfg.mifi_host, mifi_password=cfg.mifi_password or "admin",
                home_iccid=settings.home_iccid, rotation_enabled=settings.rotation_enabled)]
            self.save()
        first = self.groups[0].id
        db.x("UPDATE profiles SET device_id=? WHERE device_id IS NULL OR device_id=''", (first,))

    # ---------------------------------------------------------------- life
    def save(self) -> None:
        self.s.groups = [g.to_dict() for g in self.groups]
        self.s.save()

    def _build(self, g: GroupConfig) -> Engine:
        gdb = GroupDB(self.db, g)
        card = CardService(g.card_config(self.cfg.insecure_tls), gdb)
        card.lock = self.ble_lock
        e = Engine(g.card_config(self.cfg.insecure_tls), GroupView(self.s, g), gdb,
                   mifi=PlatformMifi(g.mifi_host, g.mifi_password), card=card, group=g)
        return e

    async def _learn_imeis(self) -> None:
        """Record each MiFi's IMEI so a re-addressed device is still recognised."""
        for g in self.groups:
            if g.mifi_imei or g.id not in self.engines:
                continue
            try:
                r = await asyncio.to_thread(self.engines[g.id].mifi._authed,
                                            {"cmd": "imei", "multi_data": 1})
                if r.get("imei"):
                    g.mifi_imei = r["imei"]
                    self.save()
            except Exception as e:  # noqa: BLE001
                log.info("IMEI of %s not read: %s", g.id, e)

    def start(self) -> None:
        self._started = True
        asyncio.get_running_loop().call_later(15, lambda: asyncio.ensure_future(self._learn_imeis()))
        for g in self.groups:
            if g.enabled and g.id not in self.engines:
                self.engines[g.id] = self._build(g)
                self.engines[g.id].start()

    async def stop(self) -> None:
        for e in list(self.engines.values()):
            await e.stop()

    async def _restart(self, g: GroupConfig) -> None:
        old = self.engines.pop(g.id, None)
        if old:
            if old.device_lock.locked():
                self.engines[g.id] = old
                raise PlatformError(f"{g.name} is busy ({old.phase}); try again in a minute")
            await old.stop()
        if g.enabled and self._started:
            self.engines[g.id] = self._build(g)
            self.engines[g.id].start()

    # -------------------------------------------------------------- lookup
    def group(self, gid: str) -> GroupConfig:
        g = next((g for g in self.groups if g.id == gid), None)
        if not g:
            raise PlatformError(f"unknown device group '{gid}'")
        return g

    def primary(self) -> Engine | None:
        for g in self.groups:
            if g.id in self.engines:
                return self.engines[g.id]
        return next(iter(self.engines.values()), None)

    def engine_for(self, key: str) -> tuple[Engine, dict]:
        p = self.db.profile(key) or self.db.profile_by_iccid(key)
        if not p or not p["on_card"]:
            raise PlatformError(f"unknown number '{key}'")
        e = self.engines.get(p.get("device_id") or "") or (
            self.primary() if len(self.engines) == 1 else None)
        if not e:
            raise PlatformError(f"the device group of '{key}' is disabled")
        return e, p

    def engine_for_hold(self, hold_id: int) -> Engine:
        for e in self.engines.values():
            if hold_id in e.holds:
                return e
        raise PlatformError("this receive session has ended")

    def all_holds(self) -> list[dict]:
        out = []
        for gid, e in self.engines.items():
            out += [{**h, "group_id": gid} for h in e.status()["holds"]]
        return sorted(out, key=lambda h: h["created"])

    def group_status(self, g: GroupConfig) -> dict:
        e = self.engines.get(g.id)
        base = {**g.public(), "group_id": g.id}
        if not e:
            return {**base, "phase": "disabled", "phase_detail": "", "current": None, "modem": {},
                    "holds": [], "last_switch": {}, "switch_target": ""}
        st = e.status()
        st["card"] = {k: (e.card_info or {}).get(k) for k in ("eid", "free_nvm")}
        return {**st, **base}

    # ------------------------------------------------------------- editing
    def _check_unique(self, g: GroupConfig) -> None:
        for o in self.groups:
            if o.id == g.id:
                continue
            if g.ble_device and o.ble_device == g.ble_device:
                raise PlatformError(f"card {g.ble_device} is already in {o.name}")
            if o.mifi_host == g.mifi_host:
                raise PlatformError(f"MiFi address {g.mifi_host} is already used by {o.name}")
            if g.mifi_imei and o.mifi_imei == g.mifi_imei:
                raise PlatformError(f"this MiFi is already in {o.name}")

    EDITABLE = ("name", "enabled", "ble_device", "ble_password", "mifi_host", "mifi_password",
                "mifi_imei", "home_iccid", "rotation_enabled")
    HOT = ("name", "home_iccid", "rotation_enabled")   # no engine restart needed

    async def add_group(self, data: dict) -> GroupConfig:
        gid = "g" + secrets.token_hex(3)
        g = GroupConfig.from_dict({**{k: v for k, v in data.items() if k in self.EDITABLE},
                                   "id": gid})
        if not g.ble_device:
            raise PlatformError("choose a 9eSIM card")
        g.name = g.name or f"设备组 {len(self.groups) + 1}"
        self._check_unique(g)
        self.groups.append(g)
        self.save()
        self.db.event("device", f"device group {g.name} added ({g.ble_device} + {g.mifi_host})")
        await self._restart(g)
        return g

    async def update_group(self, gid: str, data: dict) -> GroupConfig:
        g = self.group(gid)
        old = g.to_dict()
        for k, v in data.items():
            if k in self.EDITABLE:
                if k in ("ble_password", "mifi_password") and v == "":
                    v = None if k == "ble_password" else old[k]   # "" = keep / default
                setattr(g, k, v)
        try:
            self._check_unique(g)
        except PlatformError:
            for k, v in old.items():
                setattr(g, k, v)
            raise
        self.save()
        changed = [k for k in self.EDITABLE if old[k] != getattr(g, k)]
        if any(k not in self.HOT for k in changed):
            try:
                await self._restart(g)
            except PlatformError:
                for k, v in old.items():
                    setattr(g, k, v)
                self.save()
                raise
        elif g.id in self.engines:
            self.engines[g.id].wake()
        if changed:
            self.db.event("device", f"device group {g.name} changed: {changed}")
        return g

    async def remove_group(self, gid: str) -> None:
        g = self.group(gid)
        if len(self.groups) == 1:
            raise PlatformError("cannot remove the last device group")
        e = self.engines.get(gid)
        if e and e.device_lock.locked():
            raise PlatformError(f"{g.name} is busy; try again in a minute")
        if e:
            await e.stop()
            self.engines.pop(gid, None)
        self.groups.remove(g)
        self.save()
        self.db.event("device", f"device group {g.name} removed", "warning")
