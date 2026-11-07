"""SZXF / MF761W (ZTE goform) MiFi client: status, SMS inbox, SMS send.

Adapted from analysis/mifi-sms/mifi_sms.py, with:
  * message direction from the web UI's tag semantics
    (0 read / 1 unread = inbound, 2 sent / 3 send-failed = outbound, 4 draft);
  * automatic re-login when the session expires;
  * send result that ignores the stale status left by the previous send.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import http.cookiejar
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

log = logging.getLogger("mifi")

GSM7_DEFAULT = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓ"
    "ΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789"
    ":;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyz"
    "äöñüà\u000c ")
GSM7_EXTENDED = set("{}[]~\\^€|")

TAG_DIRECTION = {"0": "in", "1": "in", "2": "out", "3": "out", "4": "draft"}
TAG_STATUS = {"0": "read", "1": "unread", "2": "sent", "3": "failed", "4": "draft"}


class MifiError(Exception):
    pass


def sms_encoding(text: str) -> str:
    return "GSM7_default" if all(c in GSM7_DEFAULT or c in GSM7_EXTENDED for c in text) else "UNICODE"


def utf16_units(text: str) -> int:
    return len(text.encode("utf-16-be", "surrogatepass")) // 2


def decode_content(content: str) -> str:
    if content and len(content) % 4 == 0 and re.fullmatch(r"[0-9a-fA-F]+", content):
        return bytes.fromhex(content).decode("utf-16-be", errors="replace")
    return content


class Mifi:
    def __init__(self, host: str, password: str):
        self.base = "http://" + host
        self.password = password
        self._new_session()

    def _new_session(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self.jar))

    # ------------------------------------------------------------ sync core
    def _request(self, values: dict, post: bool = False, timeout: float = 10) -> dict:
        values = {"isTest": "false", **values, "_": str(time.time_ns())}
        data = urllib.parse.urlencode(values).encode()
        url = self.base + "/goform/goform_" + ("set" if post else "get") + "_cmd_process"
        req = urllib.request.Request(url if post else url + "?" + data.decode(),
                                     data=data if post else None,
                                     headers={"Referer": self.base + "/index.html"})
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return json.load(r)
        except OSError as e:
            raise MifiError(f"MiFi unreachable at {self.base}: {e}") from e

    def _login(self) -> None:
        if not self.password:
            raise MifiError("MiFi admin password not configured")
        self._new_session()
        r = self._request({"goformId": "LOGIN",
                           "password": base64.b64encode(self.password.encode()).decode()}, True)
        if r.get("result") not in ("0", "4"):
            raise MifiError(f"MiFi login failed: {r}")

    def _authed(self, values: dict, post: bool = False) -> dict:
        r = self._request(values, post)
        if self._needs_login(r):
            self._login()
            r = self._request(values, post)
        return r

    @staticmethod
    def _needs_login(r: dict) -> bool:
        return r.get("loginfo") not in (None, "ok") or r.get("result") == "failure_login"

    def status_sync(self) -> dict:
        cmd = ("loginfo,modem_main_state,pin_status,network_type,sub_network_type,network_provider,"
               "signalbar,ppp_status,sim_current_type,sim_imsi,ziccid,msisdn,sms_unread_num")
        r = self._request({"cmd": cmd, "multi_data": 1})
        if r.get("loginfo") != "ok":
            self._login()
            r = self._request({"cmd": cmd, "multi_data": 1})
        return r

    def inbox_sync(self, store: int = 1, limit: int = 500) -> list[dict]:
        r = self._authed({"cmd": "sms_data_total", "page": 0, "data_per_page": limit,
                          "mem_store": store, "tags": 10, "order_by": "order by id desc"})
        out = []
        for m in r.get("messages", []) or []:
            tag = str(m.get("tag", ""))
            out.append({
                "mifi_id": str(m.get("id", "")), "peer": m.get("number", ""),
                "body": decode_content(m.get("content", "")), "sms_time": m.get("date", ""),
                "tag": tag, "direction": TAG_DIRECTION.get(tag, "unknown"),
                "status": TAG_STATUS.get(tag, tag), "store": store,
            })
        return out

    def send_sync(self, number: str, text: str, max_units: int = 70) -> dict:
        if not re.fullmatch(r"\+?[0-9]{5,15}", number):
            raise MifiError("invalid phone number")
        units = utf16_units(text)
        if not text or units > max_units:
            raise MifiError(f"message must be 1..{max_units} UTF-16 units (single SMS)")
        st = self.status_sync()
        net = (st.get("network_type") or "").lower()
        if not net or any(s in net for s in ("no service", "limited", "searching")):
            raise MifiError(f"modem has no normal service ({st.get('network_type')}); not sent")
        before = self._authed({"cmd": "sms_cmd_status_info", "sms_cmd": 4})
        now = datetime.datetime.now(ZoneInfo("Asia/Shanghai"))
        encoding = sms_encoding(text)
        r = self._authed({"goformId": "SEND_SMS", "Number": number,
                          "sms_time": now.strftime("%y;%m;%d;%H;%M;%S;") + "+8",
                          "MessageBody": text.encode("utf-16-be", "surrogatepass").hex().upper(),
                          "ID": -1, "encode_type": encoding}, True)
        history = [before, r]
        outcome = "rejected"
        if r.get("result") == "success":
            outcome = "pending"
            deadline = time.monotonic() + 60
            seen_change = False
            while time.monotonic() < deadline:
                time.sleep(1)
                s = self._authed({"cmd": "sms_cmd_status_info", "sms_cmd": 4})
                history.append(s)
                res = s.get("sms_cmd_status_result")
                # The previous send's final state lingers; only trust it after
                # we have seen the command go through a non-final state, or
                # after a few seconds have passed.
                if res not in ("2", "3"):
                    seen_change = True
                    continue
                if seen_change or res != before.get("sms_cmd_status_result") or len(history) > 6:
                    outcome = {"2": "failed", "3": "accepted"}[res]
                    break
        return {"outcome": outcome, "encoding": encoding, "utf16_units": units,
                "responses": history,
                "note": "accepted = modem backend accepted it, not handset delivery"}

    def at_sync(self, command: str) -> str:
        r = self._authed({"goformId": "EXECUTE_AT_COMMAND", "at_cmd": command}, True)
        return r.get("result", "")

    def sms_ready_sync(self) -> str:
        """SMS subsystem init state: '3' = ready, '2' = failed, other = in progress."""
        r = self._authed({"cmd": "sms_cmd_status_info", "sms_cmd": 1})
        return str(r.get("sms_cmd_status_result", ""))

    def wifi_state_sync(self) -> dict:
        return self._authed({"cmd": "wifiEnabled,SSID1", "multi_data": 1})

    def set_wifi_sync(self, enabled: bool) -> dict:
        """Turn the MiFi's Wi-Fi hotspot on/off (USB link is unaffected)."""
        return self._authed({"goformId": "SET_WIFI_INFO", "wifiEnabled": "1" if enabled else "0"}, True)

    def reboot_sync(self) -> dict:
        """Reboot the MiFi. The session is lost; the next call logs in again."""
        r = self._authed({"goformId": "REBOOT_DEVICE"}, True)
        self._new_session()
        return r

    # ------------------------------------------------------------ async wrappers
    async def status(self) -> dict:
        return await asyncio.to_thread(self.status_sync)

    async def inbox(self, store: int = 1) -> list[dict]:
        return await asyncio.to_thread(self.inbox_sync, store)

    async def send(self, number: str, text: str) -> dict:
        return await asyncio.to_thread(self.send_sync, number, text)

    async def at(self, command: str) -> str:
        return await asyncio.to_thread(self.at_sync, command)

    async def sms_ready(self) -> str:
        return await asyncio.to_thread(self.sms_ready_sync)

    async def wifi_state(self) -> dict:
        return await asyncio.to_thread(self.wifi_state_sync)

    async def set_wifi(self, enabled: bool) -> dict:
        return await asyncio.to_thread(self.set_wifi_sync, enabled)

    async def reboot(self) -> dict:
        return await asyncio.to_thread(self.reboot_sync)


def in_service(status: dict) -> bool:
    net = (status.get("network_type") or "").lower()
    return bool(net) and not any(s in net for s in ("no service", "limited", "searching"))
