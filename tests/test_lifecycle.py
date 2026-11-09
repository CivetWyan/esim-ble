"""Offline tests for MCP-driven auto start / auto stop."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from esimble.platform import lifecycle
from esimble.platform.sdk import PlatformAPIError
from test_devices import TWO, make
from test_platform import A, B


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.p = [mock.patch.object(lifecycle, "LEASE_DIR", d / "leases"),
                  mock.patch.object(lifecycle, "MARKER", d / "platform.autostart")]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        self.tmp.cleanup()

    def test_live_and_stale_leases(self):
        # a real child process whose command line looks like an MCP server
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)",
                                  "esimble.platform", "mcp"])
        try:
            lifecycle.LEASE_DIR.mkdir(parents=True)
            (lifecycle.LEASE_DIR / f"{child.pid}.json").write_text(json.dumps({"pid": child.pid}))
            (lifecycle.LEASE_DIR / "999999.json").write_text(json.dumps({"pid": 999999}))
            (lifecycle.LEASE_DIR / f"{os.getpid()}.json").write_text(json.dumps({"pid": os.getpid()}))
            live = lifecycle.live_leases()
            # dead pid removed; this test process is alive but is not an MCP server (PID reuse guard)
            self.assertEqual([d["pid"] for d in live], [child.pid])
            self.assertEqual(sorted(p.name for p in lifecycle.LEASE_DIR.iterdir()),
                             [f"{child.pid}.json"])
        finally:
            child.kill()
            child.wait()
        self.assertEqual(lifecycle.live_leases(), [])

    def test_lease_of_console_script_launcher_counts_as_alive(self):
        # regression: .venv/bin/esim-sms-mcp has no "esimble.platform mcp" in its command line
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)",
                                  "/x/.venv/bin/esim-sms-mcp"])
        try:
            lifecycle.LEASE_DIR.mkdir(parents=True)
            (lifecycle.LEASE_DIR / f"{child.pid}.json").write_text(json.dumps({"pid": child.pid}))
            self.assertEqual([d["pid"] for d in lifecycle.live_leases()], [child.pid])
        finally:
            child.kill()
            child.wait()

    def test_register_and_remove(self):
        lifecycle.register_lease("test")
        self.assertTrue((lifecycle.LEASE_DIR / f"{os.getpid()}.json").exists())
        lifecycle.remove_lease()
        self.assertFalse((lifecycle.LEASE_DIR / f"{os.getpid()}.json").exists())

    def test_marker(self):
        self.assertIsNone(lifecycle.autostarted_pid())
        lifecycle.mark_autostarted(123)
        self.assertEqual(lifecycle.autostarted_pid(), 123)
        lifecycle.clear_marker()
        self.assertIsNone(lifecycle.autostarted_pid())


class WatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m, self.db, self.s = make(self.tmp.name, TWO)
        self.m.start()
        self.m._watchdog.cancel()
        await asyncio.sleep(0.3)

    async def asyncTearDown(self):
        await self.m.stop()
        self.tmp.cleanup()

    async def test_ready_to_stop_releases_waits_and_returns_home(self):
        e1 = self.m.engines["g1"]
        self.assertEqual(self.m.ready_to_stop(), (True, ""))
        h = e1.hold(B, 600)                       # an MCP wait on a non-home number
        for _ in range(150):
            if e1.current_iccid == B and e1.phase == "online":
                break
            await asyncio.sleep(0.1)
        ok, why = self.m.ready_to_stop()          # releases the wait; still on B
        self.assertFalse(ok)
        self.assertIn("常驻号", why)
        self.assertNotIn(h.id, e1.holds)
        for _ in range(150):                      # engine goes back home by itself
            if e1.current_iccid == A and e1.phase == "online":
                break
            await asyncio.sleep(0.1)
        self.assertEqual(self.m.ready_to_stop(), (True, ""))

    async def test_manual_session_blocks_stop(self):
        self.m.engines["g2"].hold(self.m.engines["g2"].current_iccid, 600, reason="manual")
        ok, why = self.m.ready_to_stop()
        self.assertFalse(ok)
        self.assertIn("收码会话", why)

    async def test_loop_stops_only_autostarted_idle_platform(self):
        self.s.mcp_idle_stop_seconds = 0
        kills = []
        with mock.patch.object(lifecycle, "autostarted_pid", return_value=None), \
                mock.patch("os.kill", side_effect=lambda *a: kills.append(a)):
            t = asyncio.create_task(self.m._auto_stop_loop(interval=0.05))
            await asyncio.sleep(0.3)
            self.assertEqual(kills, [])           # started by hand: never stopped
            t.cancel()
        with mock.patch.object(lifecycle, "autostarted_pid", return_value=os.getpid()), \
                mock.patch.object(lifecycle, "live_leases", return_value=[{"pid": 1}]), \
                mock.patch("os.kill", side_effect=lambda *a: kills.append(a)):
            t = asyncio.create_task(self.m._auto_stop_loop(interval=0.05))
            await asyncio.sleep(0.3)
            self.assertEqual(kills, [])           # an MCP client is connected
            self.assertEqual(self.m.auto_stop["mcp_clients"], 1)
            t.cancel()
        with mock.patch.object(lifecycle, "autostarted_pid", return_value=os.getpid()), \
                mock.patch.object(lifecycle, "live_leases", return_value=[]), \
                mock.patch.object(lifecycle, "clear_marker") as clear, \
                mock.patch("os.kill", side_effect=lambda *a: kills.append(a)):
            t = asyncio.create_task(self.m._auto_stop_loop(interval=0.05))
            await asyncio.wait_for(t, 2)
        self.assertEqual(len(kills), 1)
        clear.assert_called_once()


class AttributionTests(unittest.IsolatedAsyncioTestCase):
    async def test_messages_after_restart_keep_owner(self):
        from test_platform import make as make1
        with tempfile.TemporaryDirectory() as tmp:
            eng, db, mifi, card = make1(tmp)
            await eng.refresh_card()
            db.set_setting(f"last_online:{eng.gid}", A)   # A was online before the stop
            eng._last_online = A
            mifi.deliver("+861", "arrived while stopped 1234")
            await eng._poll()
            self.assertEqual(db.find_messages(A, "in")[0]["attribution"], "active")


class McpAutostartTests(unittest.TestCase):
    def test_call_starts_platform_once_then_retries(self):
        from esimble.platform import mcp_server as ms
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            if calls["n"] == 1:
                raise PlatformAPIError(0, "not reachable")
            return {"ok": True}

        with mock.patch.object(ms, "_ensure_platform", return_value=True) as ens:
            r = asyncio.run(ms._call(fn))
        self.assertEqual(r, {"ok": True})
        ens.assert_called_once()

    def test_no_autostart_for_http_errors_or_when_disabled(self):
        from esimble.platform import mcp_server as ms

        def fn():
            raise PlatformAPIError(404, "unknown number")

        with mock.patch.object(ms, "_ensure_platform") as ens:
            r = asyncio.run(ms._call(fn))
        self.assertEqual(r["http_status"], 404)
        ens.assert_not_called()
        with mock.patch.object(ms, "AUTOSTART", False):
            self.assertFalse(ms._ensure_platform())


if __name__ == "__main__":
    unittest.main()


class LauncherTests(unittest.TestCase):
    """Regression: Claude Code starts the MCP from the user's project directory."""

    def test_mcp_launcher_starts_from_any_directory(self):
        from esimble.config import ROOT
        exe = ROOT / ".venv" / "bin" / "esim-sms-mcp"
        if not exe.exists():
            self.skipTest("launcher not installed (run ./start.sh or pip install -e .)")
        init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"}}}) + "\n"
        with tempfile.TemporaryDirectory() as other:
            env = {**os.environ, "ESIM_PLATFORM_URL": "http://127.0.0.1:9", "ESIM_MCP_AUTOSTART": "0"}
            p = subprocess.run([str(exe)], input=init, capture_output=True, text=True, cwd=other,
                               env=env, timeout=30)
        self.assertNotIn("ModuleNotFoundError", p.stderr)
        self.assertIn('"serverInfo"', p.stdout)
