"""Scheduler: keeps one number online at a time, rotates, serves on-demand waits,
pulls the MiFi inbox into SQLite and attributes messages to the online number.

Only this process touches the card (BLE) and the MiFi; every card operation and
every switch runs under `device_lock`.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from dataclasses import dataclass, field

from ..card.euicc import CardError
from ..card_service import CardService
from ..config import Config
from ..mifi import MifiError, in_service
from .modem import PlatformMifi
from .settings import PlatformSettings
from .store import PlatformDB, iccid_match, now_iso

log = logging.getLogger("engine")

_wait_ids = itertools.count(1)


class PlatformError(Exception):
    pass


@dataclass
class Hold:
    """Somebody wants `iccid` online (an on-demand wait or a manual switch)."""
    id: int
    iccid: str
    created: float
    until: float
    reason: str
    failed: str = ""
    meta: dict = field(default_factory=dict)


class Engine:
    def __init__(self, cfg: Config, settings: PlatformSettings, db: PlatformDB,
                 mifi: PlatformMifi | None = None, card: CardService | None = None,
                 group=None):
        self.group = group
        self.gid = group.id if group else "g1"
        self.log = logging.getLogger(f"engine.{group.id}" if group else "engine")
        self.cfg = cfg
        self.s = settings
        self.db = db
        self.mifi = mifi or PlatformMifi(cfg.mifi_host, cfg.mifi_password)
        self.card = card or CardService(cfg, db)
        self.device_lock = asyncio.Lock()
        self.new_messages = asyncio.Condition()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.holds: dict[int, Hold] = {}
        # live state
        self.phase = "starting"
        self.phase_detail = ""
        self.phase_since = time.time()
        self.current_iccid: str = ""
        self.switch_target: str = ""
        self.online_since: float = 0
        self.dwell_until: float = time.time() + settings.dwell_seconds
        self.modem: dict = {}
        self.sms_ready: str = ""
        self.capacity: dict = {}
        self.last_poll_at: float = 0
        self.last_error: str = ""
        self.card_info: dict | None = None
        self._first_poll = True
        self.stats = {"stored": 0, "deleted": 0, "switches": 0, "switch_failures": 0}
        # progress of the current / last switch, for the admin page
        self.last_switch: dict = {}

    # ------------------------------------------------------------------ life
    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="engine")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def wake(self) -> None:
        self._wake.set()

    def _set_phase(self, phase: str, detail: str = "") -> None:
        if phase != self.phase or detail != self.phase_detail:
            self.log.info("phase %s %s", phase, detail)
            self.phase, self.phase_detail, self.phase_since = phase, detail, time.time()

    async def _run(self) -> None:
        try:
            await self.refresh_card()
        except Exception as e:  # noqa: BLE001
            self.log.warning("initial card read failed: %s", e)
            self.db.event("card", f"initial card read failed: {e}", "warning")
        self.dwell_until = time.time() + self.s.dwell_seconds
        try:
            await self._poll()   # learn which number is online before scheduling
        except Exception as e:  # noqa: BLE001
            self.log.warning("initial poll failed: %s", e)
        while True:
            try:
                target = self._next_target()
                if self.phase == "mifi_offline":
                    await self._poll()      # don't switch while the modem is unreachable
                elif target and not iccid_match(target, self.current_iccid or ""):
                    await self._switch(target)
                else:
                    await self._poll()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.last_error = f"{type(e).__name__}: {e}"
                self.log.exception("engine loop error")
                self._set_phase("error", self.last_error)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.s.poll_seconds)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------- scheduling
    def candidates(self) -> list[dict]:
        now = time.time()
        out = []
        for p in self.db.profiles():
            if not p["poll_enabled"] or p["profile_class"] == "test":
                continue
            if (p["last_switch_error_at"] or 0) > now - self.s.switch_retry_after_seconds \
                    and not iccid_match(p["iccid"], self.current_iccid):
                continue
            out.append(p)
        return out

    def _step(self, key: str, text: str) -> None:
        """Record a switch progress step (once per key)."""
        steps = self.last_switch.setdefault("steps", [])
        if not any(st["key"] == key for st in steps):
            steps.append({"key": key, "text": text, "ts": time.time()})

    def home_profile(self) -> dict | None:
        if not self.s.home_iccid:
            return None
        p = self.db.profile_by_iccid(self.s.home_iccid)
        return p if p and p["on_card"] else None

    def _active_holds(self) -> list[Hold]:
        now = time.time()
        for h in [h for h in self.holds.values() if h.until < now]:
            self.holds.pop(h.id, None)
        return sorted((h for h in self.holds.values() if not h.failed), key=lambda h: h.created)

    def _next_target(self) -> str:
        holds = self._active_holds()
        if holds:
            # The current number keeps priority while somebody is waiting on it.
            for h in holds:
                if iccid_match(h.iccid, self.current_iccid):
                    return self.current_iccid
            return holds[0].iccid
        if not self.s.rotation_enabled:
            # resident mode: go back to the home number when nobody needs another one
            home = self.home_profile()
            if not home:
                return ""
            if not iccid_match(home["iccid"], self.current_iccid) and \
                    (home["last_switch_error_at"] or 0) > time.time() - 120:
                return ""      # home just failed; retry in 2 minutes
            return home["iccid"]
        cands = self.candidates()
        if not cands:
            return ""
        ids = [p["iccid"] for p in cands]
        cur = next((i for i in ids if iccid_match(i, self.current_iccid)), None)
        if cur is None:
            return ids[0]
        if len(ids) == 1 or time.time() < self.dwell_until:
            return ""
        return ids[(ids.index(cur) + 1) % len(ids)]

    # ------------------------------------------------------------------- poll
    async def _poll(self, attribution: str | None = None) -> int:
        try:
            st = await self.mifi.status()
        except MifiError as e:
            self._set_phase("mifi_offline", str(e))
            return 0
        self.modem = {k: st.get(k, "") for k in (
            "modem_main_state", "network_type", "sub_network_type", "network_provider",
            "signalbar", "ppp_status", "sim_current_type", "sim_imsi", "ziccid", "msisdn",
            "sms_unread_num", "pin_status")}
        self.last_poll_at = time.time()
        zic = st.get("ziccid", "")
        prof = self.db.profile_by_iccid(zic) if zic else None
        if prof and not iccid_match(prof["iccid"], self.current_iccid):
            if self.current_iccid:
                self.log.info("modem reports %s (expected %s)", prof["iccid"], self.current_iccid)
            self.current_iccid = prof["iccid"]
            self.online_since = time.time()
        elif not prof:
            self.current_iccid = ""
        if prof:
            self._learn(prof, st)
        try:
            self.sms_ready = await self.mifi.sms_ready()
        except MifiError:
            self.sms_ready = ""
        n = await self._drain(prof, attribution)
        if self.phase in ("starting", "online", "no_service", "error", "mifi_offline", "idle"):
            if not prof:
                self._set_phase("idle", f"modem SIM {zic or '-'} is not a managed number")
            elif not in_service(st):
                self._set_phase("no_service", st.get("network_type", ""))
            elif self.sms_ready != "3":
                self._set_phase("no_service", f"sms subsystem state {self.sms_ready}")
            else:
                self._set_phase("online", prof.get("label") or prof["iccid"])
        return n

    def _learn(self, prof: dict, st: dict) -> None:
        upd = {}
        if st.get("msisdn") and not prof.get("msisdn"):
            upd["msisdn"] = st["msisdn"]
        if st.get("sim_imsi") and st["sim_imsi"] != prof.get("imsi"):
            upd["imsi"] = st["sim_imsi"]
        if in_service(st):
            upd["last_active_at"] = now_iso()
        msisdn = upd.get("msisdn") or prof.get("msisdn")
        if msisdn:
            self.db.x("UPDATE messages SET msisdn=? WHERE iccid=? AND msisdn=''",
                      (msisdn, prof["iccid"]))
        if upd:
            self.db.update_profile(prof["iccid"], **upd)
            if "msisdn" in upd:
                self.db.event("profile", f"learned number {upd['msisdn']} for {prof['iccid']}")

    async def _drain(self, prof: dict | None, attribution: str | None = None) -> int:
        """Store every inbox item, then delete what is stored from the device."""
        try:
            items = await self.mifi.inbox(1)
        except MifiError as e:
            self.log.warning("inbox read failed: %s", e)
            return 0
        attribution = attribution or ("startup" if self._first_poll else "active")
        if not prof:
            attribution = "unknown"
        self._first_poll = False
        stored, deletable = 0, []
        for m in items:
            if m["direction"] not in ("in", "out"):
                continue  # drafts stay on the device
            rid = self.db.store_modem_message(m, prof, attribution)
            if rid:
                stored += 1
                self.log.info("stored %s sms id=%s from %s for %s (%s)", m["direction"], rid,
                         m["peer"], prof["iccid"] if prof else "?", attribution)
            if rid or self.db.dedup_exists(m):
                deletable.append(m)
        if stored:
            self.stats["stored"] += stored
            async with self.new_messages:
                self.new_messages.notify_all()
        if self.s.delete_after_store and deletable:
            try:
                await self.mifi.delete([m["mifi_id"] for m in deletable])
                for m in deletable:
                    self.db.mark_deleted(m)
                self.stats["deleted"] += len(deletable)
                self.log.info("deleted %d stored sms from the MiFi", len(deletable))
            except MifiError as e:
                self.log.warning("delete from MiFi failed: %s", e)
                self.db.event("mifi", f"delete from MiFi failed: {e}", "warning")
        try:
            self.capacity = await self.mifi.capacity()
        except (MifiError, ValueError):
            pass
        return stored

    # ----------------------------------------------------------------- switch
    async def _switch(self, target: str) -> None:
        prof = self.db.profile_by_iccid(target)
        label = (prof or {}).get("label") or target
        self.switch_target = target
        t0 = time.time()
        self.last_switch = {"target": target, "label": label, "started": t0, "finished": 0,
                            "ok": None, "error": "", "steps": []}
        self.log.info("switching to %s (%s)", target, label)
        self.db.event("switch", f"switching to {label} ({target})")
        try:
            async with self.device_lock:
                self._set_phase("switching", f"draining inbox, then enabling {label}")
                await self._poll(None)  # store what belongs to the outgoing number
                # Already there? (e.g. platform restarted mid-rotation)
                if iccid_match(self.modem.get("ziccid", ""), target) and \
                        in_service(self.modem) and self.sms_ready == "3":
                    self._online(target, t0)
                    return
                self._step("drain", "已收取当前号码的短信")
                self._set_phase("switching", f"enabling {label} over Bluetooth")
                rc = await self.card.enable(target)
                self.log.info("enable rc=%s", rc)
                self._step("enable", "蓝牙启用成功")
                self._set_phase("rebooting", "rebooting the MiFi so it re-registers")
                try:
                    await self.mifi.reboot()
                except MifiError as e:
                    self.log.info("reboot call ended with %s (expected while it goes down)", e)
                # wait for it to go down (up to 60 s) and come back
                down_deadline = time.time() + 60
                while time.time() < down_deadline and await self.mifi.reachable():
                    await asyncio.sleep(2)
                while not await self.mifi.reachable():
                    if time.time() - t0 > self.s.switch_timeout_seconds:
                        raise PlatformError("MiFi did not come back after reboot")
                    await asyncio.sleep(3)
                self._step("reboot", "随身 WiFi 已重启")
            self._first_poll = False
            self._set_phase("waiting_network", f"waiting for {label} to register")
            while True:
                await self._poll("active")
                ok_sim = iccid_match(self.modem.get("ziccid", ""), target)
                if ok_sim:
                    self._step("sim", "已识别新号码")
                    if in_service(self.modem):
                        self._step("network", "已注册网络 " + (self.modem.get("network_provider") or ""))
                if ok_sim and in_service(self.modem) and self.sms_ready == "3":
                    self._step("sms", "短信模块就绪")
                    break
                if time.time() - t0 > self.s.switch_timeout_seconds:
                    raise PlatformError(
                        f"timeout: ziccid={self.modem.get('ziccid')} net="
                        f"{self.modem.get('network_type')} sms_ready={self.sms_ready}")
                self._set_phase("waiting_network",
                                f"{label}: sim={'ok' if ok_sim else self.modem.get('ziccid', '-')}"
                                f" net={self.modem.get('network_type') or '-'}"
                                f" sms={self.sms_ready or '-'} ({int(time.time() - t0)}s)")
                await asyncio.sleep(5)
            self._online(target, t0)
        except Exception as e:  # noqa: BLE001
            self.stats["switch_failures"] += 1
            msg = f"{type(e).__name__}: {e}"
            self.log.warning("switch to %s failed: %s", target, msg)
            self.db.event("switch", f"switch to {label} failed: {msg}", "error")
            self.db.update_profile(target, last_switch_error=msg, last_switch_error_at=time.time())
            for h in self.holds.values():
                if iccid_match(h.iccid, target):
                    h.failed = msg
            async with self.new_messages:
                self.new_messages.notify_all()
            self.last_error = msg
            self._set_phase("error", msg)
            self.dwell_until = time.time() + 60
            self.last_switch.update(finished=time.time(), ok=False, error=msg)
        finally:
            self.switch_target = ""

    def _online(self, target: str, t0: float) -> None:
        if self.last_switch.get("target") == target:
            self._step("online", "在线，可以接收短信")
            self.last_switch.update(finished=time.time(), ok=True)
        self.current_iccid = target
        self.online_since = time.time()
        self.dwell_until = time.time() + self.s.dwell_seconds
        self.stats["switches"] += 1
        self.db.update_profile(target, last_switch_error="", last_switch_error_at=0,
                               last_active_at=now_iso())
        prof = self.db.profile_by_iccid(target) or {}
        self.db.event("switch", f"{prof.get('label') or target} online after "
                                f"{int(time.time() - t0)}s")
        self._set_phase("online", prof.get("label") or target)

    # ------------------------------------------------------------------ holds
    def hold(self, iccid: str, seconds: int | None = None, reason: str = "wait") -> Hold:
        prof = self.require_profile(iccid)
        now = time.time()
        for h in self.holds.values():
            if iccid_match(h.iccid, prof["iccid"]) and h.reason == reason and not h.failed:
                h.until = max(h.until, now + (seconds or self.s.wait_hold_seconds))
                self.wake()
                return h
        h = Hold(next(_wait_ids), prof["iccid"], now, now + (seconds or self.s.wait_hold_seconds),
                 reason)
        self.holds[h.id] = h
        self.wake()
        return h

    def release(self, hold_id: int) -> None:
        self.holds.pop(hold_id, None)
        self.wake()

    def extend(self, hold_id: int, seconds: int) -> Hold:
        h = self.holds.get(hold_id)
        if not h:
            raise PlatformError("this receive session has ended")
        h.until = max(h.until, time.time()) + seconds
        return h

    def require_profile(self, key: str) -> dict:
        p = self.db.profile(key) or self.db.profile_by_iccid(key)
        if not p or not p["on_card"]:
            raise PlatformError(f"unknown number '{key}'")
        return p

    def eta_seconds(self, iccid: str) -> int | None:
        if iccid_match(iccid, self.current_iccid) and self.phase == "online":
            return 0
        holds = self._active_holds()
        ahead = 0
        for h in holds:
            if iccid_match(h.iccid, iccid):
                break
            if not iccid_match(h.iccid, self.current_iccid):
                ahead += 1
        per = 240
        if iccid_match(self.switch_target, iccid):
            return max(0, per - int(time.time() - self.phase_since))
        return per * (ahead + 1)

    async def wait_for_message(self, key: str, since_id: int, timeout: float,
                               sender: str | None = None, contains: str | None = None,
                               hold: bool = True) -> dict:
        prof = self.require_profile(key)
        h = self.hold(prof["iccid"]) if hold else None
        deadline = time.time() + max(0.0, min(timeout, 3600))
        while True:
            rows = self.db.find_messages(prof["iccid"], "in", since_id=since_id, limit=20,
                                         sender=sender, contains=contains)
            if rows:
                if h:
                    self.release(h.id)
                return {"status": "received", "messages": rows,
                        "cursor": max(r["id"] for r in rows), "number": self._pub(prof)}
            if h and h.failed:
                return {"status": "switch_failed", "error": h.failed, "messages": [],
                        "cursor": since_id, "number": self._pub(prof)}
            left = deadline - time.time()
            if left <= 0:
                return {"status": "pending", "messages": [], "cursor": since_id,
                        "number": self._pub(prof), "online": iccid_match(prof["iccid"],
                                                                          self.current_iccid)
                        and self.phase == "online",
                        "phase": self.phase, "phase_detail": self.phase_detail,
                        "eta_seconds": self.eta_seconds(prof["iccid"]),
                        "hold_until": h.until if h else None}
            async with self.new_messages:
                try:
                    await asyncio.wait_for(self.new_messages.wait(), timeout=min(left, 15))
                except asyncio.TimeoutError:
                    pass

    @staticmethod
    def _pub(p: dict) -> dict:
        return {"iccid": p["iccid"], "label": p["label"], "msisdn": p["msisdn"],
                "provider": p["card_provider"]}

    # --------------------------------------------------------------- card ops
    async def refresh_card(self) -> dict:
        async with self.device_lock:
            self.card_info = await self.card.refresh()
        return self.card_info

    async def add_profile(self, code: str, nickname: str | None, cc: str | None) -> dict:
        async with self.device_lock:
            r = await self.card.add(code, nickname or None, cc or None)
            self.card_info = await self.card.refresh()
        self.wake()
        return r

    async def delete_profile(self, iccid: str) -> None:
        if iccid_match(iccid, self.current_iccid):
            raise PlatformError("this number is online now; switch to another number first")
        async with self.device_lock:
            await self.card.delete(iccid)
            self.card_info = await self.card.refresh()
        for h in [h for h in self.holds.values() if iccid_match(h.iccid, iccid)]:
            self.holds.pop(h.id, None)

    async def rename_profile(self, iccid: str, nickname: str) -> None:
        async with self.device_lock:
            await self.card.set_nickname(iccid, nickname)
            self.card_info = await self.card.refresh()

    # ------------------------------------------------------------------- send
    async def send(self, key: str, to: str, text: str, via: str) -> dict:
        prof = self.require_profile(key)
        if not (iccid_match(prof["iccid"], self.current_iccid) and self.phase == "online"):
            raise PlatformError("number is not online; switch to it (or wait for it) first")
        mid = self.db.add_sent(prof["iccid"], prof["msisdn"], to, text, "pending", via)
        try:
            r = await self.mifi.send(to, text)
        except MifiError as e:
            self.db.set_message_status(mid, "failed")
            raise PlatformError(str(e)) from e
        self.db.set_message_status(mid, r["outcome"])
        self.db.event("sms", f"send to {to} via {via} from {prof['iccid']}: {r['outcome']}")
        return {"id": mid, "outcome": r["outcome"], "note": r["note"]}

    # ----------------------------------------------------------------- status
    def status(self) -> dict:
        prof = self.db.profile_by_iccid(self.current_iccid) if self.current_iccid else None
        return {
            "phase": self.phase, "phase_detail": self.phase_detail,
            "phase_since": self.phase_since,
            "current": self._pub(prof) if prof else None,
            "switch_target": self.switch_target,
            "online_since": self.online_since, "dwell_until": self.dwell_until,
            "rotation_enabled": self.s.rotation_enabled,
            "modem": self.modem, "sms_ready": self.sms_ready, "capacity": {
                k: self.capacity.get(k) for k in ("total", "used")},
            "last_poll_at": self.last_poll_at, "last_error": self.last_error,
            "card_busy": self.card.busy,
            "group_id": self.gid,
            "holds": [{"id": h.id, "iccid": h.iccid, "reason": h.reason, "until": h.until,
                       "created": h.created, "failed": h.failed}
                      for h in sorted(self.holds.values(), key=lambda h: h.created)
                      if h.until > time.time()],
            "stats": self.stats, "now": time.time(),
            "home": self._pub(self.home_profile()) if self.home_profile() else None,
            "last_switch": self.last_switch,
        }
