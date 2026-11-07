"""Serialized Bluetooth operations on the 9eSIM card.

Every operation opens a fresh BLE session (scan, connect, login, select ISD-R),
does its work and releases the link, so the host modem can refresh the SIM
right after an enable/disable.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import asdict

from .card.download import ActivationCode, download_profile
from .card.euicc import CardError, Euicc
from .card.nble import NbleTransport
from .config import Config
from .db import Database

log = logging.getLogger("card")


class CardService:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self.lock = asyncio.Lock()
        self.busy: str | None = None          # human readable current operation
        self.last_list: dict | None = None
        self.last_list_at: float = 0

    @asynccontextmanager
    async def session(self, what: str):
        async with self.lock:
            self.busy = what
            t0 = time.monotonic()
            transport = NbleTransport(self.cfg.ble_device, self.cfg.ble_password)
            card = Euicc(transport)
            try:
                for attempt in range(2):
                    try:
                        await transport.open()
                        await card.open_isdr()
                        break
                    except CardError as e:
                        # 6999/6985: card mid profile switch; give the modem time.
                        if attempt == 0 and e.sw in (0x6999, 0x6985):
                            log.info("ISD-R busy (%s), retrying in 20s", e)
                            await transport.close()
                            await asyncio.sleep(20)
                            continue
                        raise
                    except Exception as e:  # noqa: BLE001 - scan miss etc.
                        if attempt == 0:
                            log.info("BLE open failed (%s), retrying in 3s", e)
                            await transport.close()
                            await asyncio.sleep(3)
                            continue
                        raise
                yield card
            finally:
                try:
                    await card.close_isdr()
                except Exception:  # noqa: BLE001
                    pass
                await transport.close()
                log.info("%s took %.1fs", what, time.monotonic() - t0)
                self.busy = None

    async def refresh(self) -> dict:
        async with self.session("read card") as card:
            eid = await card.get_eid()
            free = await card.free_nvm()
            profiles = await card.list_profiles()
        self.db.sync_card_profiles(profiles)
        self.last_list = {"eid": eid, "free_nvm": free,
                          "profiles": [asdict(p) for p in profiles]}
        self.last_list_at = time.time()
        return self.last_list

    async def add(self, activation_code: str, nickname: str | None = None,
                  confirmation_code: str | None = None) -> dict:
        ac = ActivationCode.parse(activation_code)
        async with self.session("install profile") as card:
            existing = {p.iccid for p in await card.list_profiles()}

            def confirm(meta):
                return not (meta.iccid and meta.iccid in existing)

            result = await download_profile(card, ac, confirmation_code,
                                            insecure_tls=self.cfg.insecure_tls, confirm=confirm)
            if nickname:
                await card.set_nickname(result.iccid, nickname)
            profiles = await card.list_profiles()
        self.db.sync_card_profiles(profiles)
        if nickname:
            self.db.update_profile(result.iccid, label=nickname)
        self.db.event("card", f"installed profile {result.iccid} from {ac.smdp}")
        return {"iccid": result.iccid}

    async def delete(self, iccid: str) -> None:
        async with self.session("delete profile") as card:
            profiles = await card.list_profiles()
            target = next((p for p in profiles if p.iccid == iccid), None)
            if target is None:
                raise CardError(f"{iccid} is not on the card")
            if target.state == "enabled":
                raise CardError("profile is enabled; disable it (or switch to another) first")
            rc = await card.delete_profile(iccid)
            if rc != 0:
                raise CardError(f"delete returned {rc}")
            profiles = await card.list_profiles()
        self.db.sync_card_profiles(profiles)
        self.db.event("card", f"deleted profile {iccid}", "warning")

    async def set_nickname(self, iccid: str, nickname: str) -> None:
        async with self.session("rename profile") as card:
            rc = await card.set_nickname(iccid, nickname)
            if rc != 0:
                raise CardError(f"set nickname returned {rc}")
        self.db.update_profile(iccid, card_nickname=nickname)

    async def enable(self, iccid: str) -> int:
        """Enable a profile. rc 2 (already enabled) is treated as success."""
        async with self.session(f"enable {iccid}") as card:
            rc = await card.enable_profile(iccid, refresh=True)
        if rc not in (0, 2):
            raise CardError(f"enable returned {rc}")
        for p in self.db.profiles():
            self.db.update_profile(p["iccid"], card_state="enabled" if p["iccid"] == iccid
                                   else ("disabled" if p["card_state"] == "enabled" else p["card_state"]))
        return rc

    async def disable(self, iccid: str) -> int:
        async with self.session(f"disable {iccid}") as card:
            rc = await card.disable_profile(iccid, refresh=True)
        if rc not in (0, 2):
            raise CardError(f"disable returned {rc}")
        self.db.update_profile(iccid, card_state="disabled")
        return rc
