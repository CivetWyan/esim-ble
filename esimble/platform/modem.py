"""MiFi extensions the platform needs (subclass; esimble.mifi is unchanged)."""

from __future__ import annotations

import asyncio

from ..mifi import Mifi, MifiError


class PlatformMifi(Mifi):
    def delete_sync(self, mifi_ids: list[str]) -> dict:
        ids = [str(i) for i in mifi_ids if str(i).isdigit()]
        if not ids:
            return {"result": "noop"}
        r = self._authed({"goformId": "DELETE_SMS", "msg_id": ";".join(ids) + ";",
                          "notCallback": "true"}, True)
        if r.get("result") != "success":
            raise MifiError(f"DELETE_SMS failed: {r}")
        return r

    def capacity_sync(self) -> dict:
        r = self._authed({"cmd": "sms_capacity_info"})
        total = int(r.get("sms_nv_total") or 0)
        used = sum(int(r.get(k) or 0) for k in
                   ("sms_nv_rev_total", "sms_nv_send_total", "sms_nv_draftbox_total"))
        return {"total": total, "used": used, "raw": r}

    def reachable_sync(self) -> bool:
        try:
            self._request({"cmd": "modem_main_state"}, timeout=3)
            return True
        except MifiError:
            return False

    async def delete(self, mifi_ids: list[str]) -> dict:
        return await asyncio.to_thread(self.delete_sync, mifi_ids)

    async def capacity(self) -> dict:
        return await asyncio.to_thread(self.capacity_sync)

    async def reachable(self) -> bool:
        return await asyncio.to_thread(self.reachable_sync)
