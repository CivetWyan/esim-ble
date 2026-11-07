"""Offline tests for the SMS platform (no card, no MiFi)."""

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from esimble.card.euicc import Profile
from esimble.config import Config
from esimble.platform.engine import Engine
from esimble.platform.settings import PlatformSettings
from esimble.platform.store import PlatformDB, extract_code, iccid_match

A, B = "89852000000000000011", "8999500000000000022"


class FakeMifi:
    def __init__(self):
        self.ziccid = A
        self.box: list[dict] = []
        self.deleted: list[str] = []
        self.reboots = 0
        self.down_polls = 0
        self.pending_sim = None
        self.next_id = 1
        self.sent = []

    def deliver(self, peer, body, tag="1"):
        self.box.insert(0, {"mifi_id": str(self.next_id), "peer": peer, "body": body,
                            "sms_time": f"t{self.next_id}", "tag": tag,
                            "direction": {"0": "in", "1": "in", "4": "draft"}.get(tag, "out"),
                            "status": "unread", "store": 1})
        self.next_id += 1

    async def status(self):
        return {"loginfo": "ok", "network_type": "LTE", "ziccid": self.ziccid,
                "network_provider": "Demo Telecom", "signalbar": "4",
                "msisdn": "+15550001" if self.ziccid == A else "", "sim_imsi": "4540"}

    async def sms_ready(self):
        return "3"

    async def inbox(self, store=1):
        return list(self.box)

    async def delete(self, ids):
        self.deleted += ids
        self.box = [m for m in self.box if m["mifi_id"] not in ids]
        return {"result": "success"}

    async def capacity(self):
        return {"total": 20, "used": len(self.box)}

    async def reboot(self):
        self.reboots += 1
        self.down_polls = 2
        if self.pending_sim:
            self.ziccid, self.pending_sim = self.pending_sim, None
        return {}

    async def reachable(self):
        if self.down_polls:
            self.down_polls -= 1
            return False
        return True

    async def send(self, to, text):
        self.sent.append((to, text))
        return {"outcome": "accepted", "note": "n"}


class FakeCard:
    def __init__(self, db, mifi):
        self.db, self.mifi, self.busy = db, mifi, None
        self.state = {A: "enabled", B: "disabled"}

    def _profiles(self):
        return [Profile(A, self.state[A], "WEBBING", "Saily", "", "operational"),
                Profile(B, self.state[B], "Cellfie GE 2", "Cellfie", "", "operational")]

    async def refresh(self):
        self.db.sync_card_profiles(self._profiles())
        return {"eid": "E", "free_nvm": 1, "profiles": []}

    async def enable(self, iccid):
        for k in self.state:
            self.state[k] = "enabled" if k == iccid else "disabled"
        self.mifi.pending_sim = iccid
        return 0


def make(tmp, **kw):
    db = PlatformDB(Path(tmp) / "t.db")
    s = PlatformSettings(api_token="tok", poll_seconds=1, dwell_seconds=3600,
                         switch_timeout_seconds=30, **kw)
    s.save = lambda *a: None
    mifi = FakeMifi()
    card = FakeCard(db, mifi)
    eng = Engine(Config(), s, db, mifi=mifi, card=card)
    return eng, db, mifi, card


class StoreTests(unittest.TestCase):
    def test_iccid_match(self):
        self.assertTrue(iccid_match(A, A + "F"))
        self.assertTrue(iccid_match(B, B + "4"))   # dropped Luhn digit
        self.assertFalse(iccid_match(A, B))
        self.assertFalse(iccid_match("", A))

    def test_extract_code(self):
        self.assertEqual(extract_code("【某某】您的验证码是 482913，5分钟内有效"), "482913")
        self.assertEqual(extract_code("Your code: 7731"), "7731")
        self.assertEqual(extract_code("G-552014 is your Google verification code"), "552014")
        self.assertEqual(extract_code("没有数字"), "")
        self.assertEqual(extract_code("请把我的这个验证码给用户看：520cc，2026年11月7日12.53"), "520cc")
        self.assertEqual(extract_code("Your verification code is 7731."), "7731")
        self.assertEqual(extract_code("2026年11月7日 订单 12.53 元"), "")
        self.assertEqual(extract_code("123456"), "123456")


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eng, self.db, self.mifi, self.card = make(self.tmp.name)
        await self.eng.refresh_card()

    async def asyncTearDown(self):
        await self.eng.stop()
        self.tmp.cleanup()

    async def test_poll_stores_attributes_deletes_and_learns(self):
        self.mifi.deliver("+861", "old 1111")
        self.mifi.deliver("+861", "draft", tag="4")
        await self.eng._poll()
        self.mifi.deliver("+862", "验证码 654321")
        await self.eng._poll()
        rows = self.db.find_messages(A, "in")
        self.assertEqual([r["attribution"] for r in rows], ["active", "startup"])
        self.assertEqual(rows[0]["code"], "654321")
        self.assertEqual(self.mifi.deleted, ["1", "3"])        # draft stays
        self.assertEqual(len(self.mifi.box), 1)
        self.assertEqual(self.db.profile(A)["msisdn"], "+15550001")
        self.assertEqual(self.eng.phase, "online")
        await self.eng._poll()                                   # no duplicates
        self.assertEqual(len(self.db.find_messages(A)), 2)

    async def test_no_delete_when_disabled(self):
        self.eng.s.delete_after_store = False
        self.mifi.deliver("+861", "x 1234")
        await self.eng._poll()
        await self.eng._poll()
        self.assertEqual(self.mifi.deleted, [])
        self.assertEqual(len(self.db.find_messages(A)), 1)

    async def test_rotation_and_switch(self):
        self.eng.s.rotation_enabled = True
        await self.eng._poll()
        self.assertEqual(self.eng._next_target(), "")            # dwell not over
        self.eng.dwell_until = time.time() - 1
        self.assertEqual(self.eng._next_target(), B)
        self.mifi.deliver("+861", "for A 1111")                  # arrives before switch
        await self.eng._switch(B)
        self.assertEqual(self.eng.current_iccid, B)
        self.assertEqual(self.mifi.reboots, 1)
        self.assertEqual(self.eng.phase, "online")
        self.assertEqual(len(self.db.find_messages(A, "in")), 1)  # drained to A
        self.mifi.deliver("+862", "for B 2222")
        await self.eng._poll()
        self.assertEqual(self.db.find_messages(B, "in")[0]["body"], "for B 2222")

    async def test_poll_disabled_number_not_in_rotation(self):
        self.eng.s.rotation_enabled = True
        self.db.update_profile(B, poll_enabled=0)
        await self.eng._poll()
        self.eng.dwell_until = time.time() - 1
        self.assertEqual(self.eng._next_target(), "")

    async def test_wait_triggers_switch_and_returns_code(self):
        self.eng.s.rotation_enabled = False
        self.eng.start()
        await asyncio.sleep(0.2)
        cursor = self.db.max_message_id()

        async def later():
            while self.eng.current_iccid != B or self.eng.phase != "online":
                await asyncio.sleep(0.1)
            self.mifi.deliver("10690", "您的验证码为 908172")
            self.eng.wake()

        t = asyncio.create_task(later())
        r = await asyncio.wait_for(self.eng.wait_for_message(B, cursor, 20), 25)
        await t
        self.assertEqual(r["status"], "received")
        self.assertEqual(r["messages"][0]["code"], "908172")
        self.assertEqual(self.mifi.reboots, 1)
        self.assertEqual(self.eng.holds, {})                    # hold released

    async def test_startup_does_not_switch_away_from_online_number(self):
        self.eng.start()
        await asyncio.sleep(0.5)
        self.assertEqual(self.eng.current_iccid, A)
        self.assertEqual(self.mifi.reboots, 0)

    async def test_resident_mode_returns_home_after_receive(self):
        self.eng.s.home_iccid = A
        await self.eng._poll()
        self.assertEqual(self.eng._next_target(), A)            # stays home
        h = self.eng.hold(B, 60, reason="manual")
        self.assertEqual(self.eng._next_target(), B)
        await self.eng._switch(B)
        self.assertEqual([st["key"] for st in self.eng.last_switch["steps"]],
                         ["drain", "enable", "reboot", "sim", "network", "sms", "online"])
        self.assertTrue(self.eng.last_switch["ok"])
        self.assertEqual(self.eng._next_target(), B)            # held
        self.eng.extend(h.id, 60)
        self.eng.release(h.id)
        self.assertEqual(self.eng._next_target(), A)            # back home
        self.assertIsNone(self.eng.home_profile() and None)

    async def test_no_home_no_rotation_stays(self):
        await self.eng._poll()
        self.assertEqual(self.eng._next_target(), "")

    async def test_wait_pending_reports_eta(self):
        self.eng.s.rotation_enabled = False
        r = await self.eng.wait_for_message(A, self.db.max_message_id(), 0.1, hold=False)
        self.assertEqual(r["status"], "pending")


class ApiTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from esimble.platform.api import create_app
        self.tmp = tempfile.TemporaryDirectory()
        self.eng, self.db, self.mifi, self.card = make(self.tmp.name)
        asyncio.run(self.eng.refresh_card())
        asyncio.run(self.eng._poll())
        self.c = TestClient(create_app(self.eng, manage_engine=False), base_url="http://127.0.0.1")
        self.h = {"Authorization": "Bearer tok"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_auth(self):
        self.assertEqual(self.c.get("/api/v1/numbers").status_code, 401)
        self.assertEqual(self.c.get("/api/v1/numbers", headers={"Authorization": "Bearer x"}).status_code, 401)
        self.assertEqual(self.c.get("/api/v1/numbers", headers=self.h).status_code, 200)
        self.assertIn("API token", self.c.get("/").text)
        r = self.c.get("/login?token=tok", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.c.get("/api/v1/status").status_code, 200)          # cookie
        self.assertEqual(self.c.post("/api/v1/poll").status_code, 403)           # no CSRF header
        self.assertEqual(self.c.post("/api/v1/poll", headers={"X-Requested-With": "esim-admin"}).status_code, 200)

    def test_host_guard(self):
        r = self.c.get("/api/v1/numbers", headers={**self.h, "Host": "evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_numbers_patch_and_lookup(self):
        r = self.c.get("/api/v1/numbers", headers=self.h).json()
        self.assertEqual({n["iccid"] for n in r}, {A, B})
        self.assertTrue(next(n for n in r if n["iccid"] == A)["online"])
        r = self.c.patch(f"/api/v1/numbers/{B}", json={"label": "格鲁吉亚", "msisdn": "+995 555"},
                         headers=self.h)
        self.assertEqual(r.json()["msisdn"], "+995555")
        self.assertEqual(self.c.get("/api/v1/numbers/格鲁吉亚", headers=self.h).json()["iccid"], B)
        self.assertEqual(self.c.get("/api/v1/numbers/nope", headers=self.h).status_code, 404)

    def test_delete_requires_confirm_and_offline(self):
        self.assertEqual(self.c.delete(f"/api/v1/numbers/{B}", headers=self.h).status_code, 400)
        r = self.c.delete(f"/api/v1/numbers/{A}?confirm={A}", headers=self.h)
        self.assertEqual(r.status_code, 400)
        self.assertIn("online", r.json()["detail"])

    def test_send_permissions(self):
        body = {"number": A, "to": "+8613800000000", "text": "hi"}
        self.assertEqual(self.c.post("/api/v1/send", json=body, headers=self.h).status_code, 403)
        self.eng.s.api_allow_send = True
        r = self.c.post("/api/v1/send", json=body, headers=self.h)
        self.assertEqual(r.json()["outcome"], "accepted")
        out = self.db.find_messages(A, "out")
        self.assertEqual(out[0]["via"], "rest")
        r = self.c.post("/api/v1/send", json={**body, "number": B}, headers=self.h)
        self.assertEqual(r.status_code, 400)                     # B not online

    def test_messages_and_latest_code(self):
        self.mifi.deliver("10690", "验证码 135790")
        asyncio.run(self.eng._poll())
        r = self.c.get("/api/v1/messages/latest-code", params={"number": A}, headers=self.h).json()
        self.assertEqual(r["code"], "135790")
        r = self.c.get("/api/v1/messages", params={"number": A, "contains": "1357"}, headers=self.h)
        self.assertEqual(len(r.json()), 1)
        r = self.c.post("/api/v1/wait", json={"number": A, "since_id": 0, "timeout": 1},
                        headers=self.h).json()
        self.assertEqual(r["status"], "received")
        self.assertEqual(r["code"], "135790")


if __name__ == "__main__":
    unittest.main()
