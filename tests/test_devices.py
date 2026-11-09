"""Offline tests for device groups (multi card + multi MiFi), discovery helpers."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from esimble.card.euicc import Profile
from esimble.config import Config
from esimble.platform import devices
from esimble.platform.devices import GroupConfig, GroupDB, GroupView
from esimble.platform.engine import Engine
from esimble.platform.manager import Manager
from esimble.platform.settings import PlatformSettings
from esimble.platform.store import PlatformDB
from test_platform import A, B, FakeCard, FakeMifi

C = "89860000000000000033"


class FakeCard2(FakeCard):
    def __init__(self, db, mifi):
        super().__init__(db, mifi)
        self.state = {C: "enabled"}

    def _profiles(self):
        return [Profile(C, self.state[C], "CMCC", "CMCC", "", "operational")]


class FakeManager(Manager):
    def _build(self, g):
        gdb = GroupDB(self.db, g)
        mifi = FakeMifi()
        if g.id == "g2":
            mifi.ziccid = C
            card = FakeCard2(gdb, mifi)
        else:
            card = FakeCard(gdb, mifi)
        return Engine(Config(), GroupView(self.s, g), gdb, mifi=mifi, card=card, group=g)


def make(tmp, groups=None):
    db = PlatformDB(Path(tmp) / "t.db")
    s = PlatformSettings(api_token="tok", poll_seconds=1, groups=groups or [])
    s.save = lambda *a: None
    cfg = Config(ble_device="9eSIM000000", mifi_host="192.168.0.1", mifi_password="pw")
    return FakeManager(cfg, s, db), db, s


TWO = [GroupConfig(id="g1", name="一号", ble_device="9eSIM000000", home_iccid=A).to_dict(),
       GroupConfig(id="g2", name="二号", ble_device="9eSIM111111", mifi_host="192.168.1.1",
                   mifi_imei="2").to_dict()]


class GroupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_migrates_single_device_config(self):
        db = PlatformDB(Path(self.tmp.name) / "m.db")
        db.x("INSERT INTO profiles(iccid, on_card) VALUES (?, 1)", (A,))
        s = PlatformSettings(api_token="t", home_iccid=A, rotation_enabled=True)
        s.save = lambda *a: None
        m = FakeManager(Config(ble_device="9eSIM000000", mifi_password=""), s, db)
        g = m.groups[0]
        self.assertEqual((g.id, g.ble_device, g.mifi_password, g.home_iccid, g.rotation_enabled),
                         ("g1", "9eSIM000000", "admin", A, True))
        self.assertEqual(s.groups[0]["id"], "g1")
        self.assertEqual(db.profile(A)["device_id"], "g1")
        self.assertNotIn("groups", s.public())

    async def test_group_db_sync_is_scoped(self):
        m, db, _ = make(self.tmp.name, TWO)
        m.start()
        await asyncio.sleep(0.3)
        e1, e2 = m.engines["g1"], m.engines["g2"]
        self.assertEqual({p["iccid"] for p in e1.db.profiles()}, {A, B})
        self.assertEqual({p["iccid"] for p in e2.db.profiles()}, {C})
        await e1.refresh_card()                          # must not remove C
        self.assertTrue(db.profile(C)["on_card"])
        self.assertEqual(m.engine_for(C)[0], e2)
        self.assertEqual(m.engine_for(B)[0], e1)
        # both groups online at the same time, each on its own number
        self.assertEqual((e1.current_iccid, e2.current_iccid), (A, C))
        self.assertEqual(e1.s.home_iccid, A)
        self.assertEqual(e2.s.home_iccid, "")
        self.assertEqual(e2.s.poll_seconds, 1)          # global fallback
        await m.stop()

    async def test_holds_route_to_owning_group(self):
        m, db, _ = make(self.tmp.name, TWO)
        m.start()
        await asyncio.sleep(0.3)
        e1, e2 = m.engines["g1"], m.engines["g2"]
        h = e1.hold(B, 60, reason="manual")
        self.assertIs(m.engine_for_hold(h.id), e1)
        self.assertEqual([x["group_id"] for x in m.all_holds()], ["g1"])
        for _ in range(150):
            if e1.current_iccid == B and e1.phase == "online":
                break
            await asyncio.sleep(0.1)
        self.assertEqual(e1.current_iccid, B)            # g1 switched
        self.assertEqual(e2.current_iccid, C)            # g2 untouched
        await m.stop()

    async def test_add_update_remove_group(self):
        m, db, s = make(self.tmp.name, TWO[:1])
        with self.assertRaises(Exception):
            await m.add_group({"ble_device": "9eSIM000000", "mifi_host": "192.168.5.1"})
        with self.assertRaises(Exception):
            await m.add_group({"ble_device": "9eSIM222222", "mifi_host": "192.168.0.1"})
        g = await m.add_group({"ble_device": "9eSIM222222", "mifi_host": "192.168.5.1",
                               "mifi_password": "admin"})
        self.assertEqual(len(s.groups), 2)
        self.assertNotIn("mifi_password", g.public())
        await m.update_group(g.id, {"name": "新组", "mifi_password": ""})   # "" keeps password
        self.assertEqual((g.name, g.mifi_password), ("新组", "admin"))
        await m.remove_group(g.id)
        with self.assertRaises(Exception):
            await m.remove_group("g1")                   # last group stays
        self.assertEqual([x["id"] for x in s.groups], ["g1"])


class DiscoveryTests(unittest.TestCase):
    IFACES = [
        {"iface": "en0", "ip": "192.168.31.5", "netmask": "255.255.255.0",
         "subnet": "192.168.31.0/24", "router": "192.168.31.1"},
        {"iface": "en10", "ip": "192.168.0.100", "netmask": "255.255.255.0",
         "subnet": "192.168.0.0/24", "router": "192.168.0.1"},
        {"iface": "en11", "ip": "192.168.0.101", "netmask": "255.255.255.0",
         "subnet": "192.168.0.0/24", "router": "192.168.0.1"},
    ]

    def test_scan_flags_conflict_and_suggests_free_subnet(self):
        probe = lambda h: {"modem_main_state": "x", "network_type": "LTE"} if h == "192.168.0.1" else None
        with mock.patch.object(devices, "local_interfaces", return_value=self.IFACES), \
                mock.patch.object(devices, "probe_mifi_public", side_effect=probe):
            r = devices.scan_mifis([GroupConfig(id="g1", name="一号", mifi_host="192.168.0.1")])
        self.assertEqual([m["host"] for m in r["mifis"]], ["192.168.0.1"])
        self.assertTrue(r["mifis"][0]["conflict"])
        self.assertEqual(r["mifis"][0]["group_id"], "g1")
        self.assertEqual(r["conflicts"][0]["interfaces"], ["en10", "en11"])
        self.assertEqual(r["suggested_host"], "192.168.1.1")

    def test_linux_interfaces_from_ip_json(self):
        addr = json.dumps([
            {"ifname": "lo", "addr_info": [{"local": "127.0.0.1", "prefixlen": 8}]},
            {"ifname": "wlp3s0", "addr_info": [{"local": "192.168.31.4", "prefixlen": 24}]},
            {"ifname": "enx344b50000000", "addr_info": [{"local": "192.168.0.100", "prefixlen": 24}]}])
        route = json.dumps([{"dst": "default", "gateway": "192.168.31.1", "dev": "wlp3s0"},
                            {"dst": "192.168.0.0/24", "dev": "enx344b50000000"}])
        with mock.patch.object(devices, "_run", side_effect=lambda *a: addr if "addr" in a else route):
            r = devices._linux_interfaces()
        self.assertEqual([(i["iface"], i["subnet"], i["router"]) for i in r],
                         [("wlp3s0", "192.168.31.0/24", "192.168.31.1"),
                          ("enx344b50000000", "192.168.0.0/24", "")])

    def test_relocate_rejects_bad_address(self):
        for bad in ("8.8.8.1", "192.168.1.5", "fe80::1"):
            with self.assertRaises(ValueError):
                devices.relocate_mifi("192.168.0.1", "admin", bad)


class ApiGroupTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from esimble.platform.api import create_app
        self.tmp = tempfile.TemporaryDirectory()
        self.m, self.db, _ = make(self.tmp.name, TWO)
        self.c = TestClient(create_app(self.m), base_url="http://127.0.0.1")
        self.c.__enter__()                                # runs lifespan -> engines start
        self.h = {"Authorization": "Bearer tok"}
        import time
        time.sleep(0.5)

    def tearDown(self):
        self.c.__exit__(None, None, None)
        self.tmp.cleanup()

    def test_status_numbers_and_permissions(self):
        st = self.c.get("/api/v1/status", headers=self.h).json()
        self.assertEqual([g["group_id"] for g in st["groups"]], ["g1", "g2"])
        self.assertNotIn("mifi_password", st["groups"][0])
        nums = {n["iccid"]: n for n in self.c.get("/api/v1/numbers", headers=self.h).json()}
        self.assertEqual((nums[C]["device_id"], nums[C]["group_name"], nums[C]["online"]),
                         ("g2", "二号", True))
        self.assertTrue(nums[A]["online"] and nums[A]["is_home"])
        r = self.c.post(f"/api/v1/numbers/{C}/prepare", headers=self.h).json()
        self.assertEqual((r["group_id"], r["online"]), ("g2", True))
        for path in ("/api/v1/discover/mifis", "/api/v1/groups"):
            self.assertEqual(self.c.request("POST" if path.endswith("groups") else "GET", path,
                                            headers=self.h, json={}).status_code, 403)

    def test_web_can_patch_group_home(self):
        self.c.get("/login?token=tok", follow_redirects=False)
        hd = {"X-Requested-With": "esim-admin"}
        r = self.c.patch("/api/v1/groups/g2", json={"home_iccid": C}, headers=hd)
        self.assertEqual(r.json()["home_iccid"], C)
        nums = {n["iccid"]: n for n in self.c.get("/api/v1/numbers").json()}
        self.assertTrue(nums[C]["is_home"])
        self.assertFalse(nums[A]["is_home"] and nums[A]["device_id"] == "g2")


if __name__ == "__main__":
    unittest.main()


class ApiProbeBodyTests(ApiGroupTests):
    test_status_numbers_and_permissions = None
    test_web_can_patch_group_home = None
    def test_probe_endpoints_accept_json_body(self):
        self.c.get("/login?token=tok", follow_redirects=False)
        hd = {"X-Requested-With": "esim-admin"}
        with mock.patch.object(devices, "probe_mifi", return_value={"host": "h", "imei": "2"}):
            r = self.c.post("/api/v1/discover/mifis/test", json={"host": "192.168.1.1"}, headers=hd)
        self.assertEqual((r.status_code, r.json()["group_id"]), (200, "g2"))
        r = self.c.post("/api/v1/discover/mifis/relocate", json={"host": "192.168.1.1",
                        "new_host": "192.168.7.1"}, headers=hd)
        self.assertEqual(r.status_code, 409)              # used by a running group
