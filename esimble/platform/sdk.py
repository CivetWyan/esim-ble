"""Python SDK for the SMS platform (standard library only).

    from esimble.platform.sdk import SmsPlatform
    p = SmsPlatform()                      # reads URL + token from data/platform.json
    n = p.prepare("Saily")                 # start bringing the number online, get a cursor
    # ... trigger the website to send the SMS ...
    r = p.wait_for_code("Saily", since_id=n["cursor"], timeout=600)
    print(r["code"])
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class PlatformAPIError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status, self.detail = status, detail


def _local_defaults() -> tuple[str, str]:
    from .settings import PLATFORM_PATH
    if PLATFORM_PATH.exists():
        raw = json.loads(PLATFORM_PATH.read_text())
        return f"http://127.0.0.1:{raw.get('port', 8787)}", raw.get("api_token", "")
    return "http://127.0.0.1:8787", ""


class SmsPlatform:
    def __init__(self, base_url: str | None = None, token: str | None = None,
                 client: str = "sdk"):
        d_url, d_tok = (None, None)
        if not (base_url or os.environ.get("ESIM_PLATFORM_URL")) or \
                not (token or os.environ.get("ESIM_PLATFORM_TOKEN")):
            d_url, d_tok = _local_defaults()
        self.base = (base_url or os.environ.get("ESIM_PLATFORM_URL") or d_url).rstrip("/")
        self.token = token or os.environ.get("ESIM_PLATFORM_TOKEN") or d_tok
        self.client = client
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _call(self, method: str, path: str, params: dict | None = None,
              body: Any = None, timeout: float = 30) -> Any:
        url = self.base + "/api/v1" + path
        if params:
            q = {k: v for k, v in params.items() if v not in (None, "")}
            if q:
                url += "?" + urllib.parse.urlencode(q)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "X-Client": self.client,
            "Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                detail = json.load(e).get("detail", "")
            except Exception:  # noqa: BLE001
                detail = e.reason
            raise PlatformAPIError(e.code, str(detail)) from None
        except urllib.error.URLError as e:
            raise PlatformAPIError(0, f"platform not reachable at {self.base}: {e.reason}") from None

    # ------------------------------------------------------------ read
    def status(self) -> dict:
        return self._call("GET", "/status")

    def numbers(self, include_removed: bool = False) -> list[dict]:
        return self._call("GET", "/numbers", {"include_removed": str(include_removed).lower()})

    def number(self, key: str) -> dict:
        return self._call("GET", f"/numbers/{urllib.parse.quote(key, safe='')}")

    def messages(self, number: str = "", direction: str = "", since_id: int = 0,
                 limit: int = 50, sender: str = "", contains: str = "") -> list[dict]:
        return self._call("GET", "/messages", {"number": number, "direction": direction,
                                               "since_id": since_id, "limit": limit,
                                               "sender": sender, "contains": contains})

    def latest_code(self, number: str, since_id: int = 0, max_age_seconds: int = 1800,
                    sender: str = "") -> dict:
        return self._call("GET", "/messages/latest-code", {
            "number": number, "since_id": since_id, "max_age_seconds": max_age_seconds,
            "sender": sender})

    def events(self, limit: int = 100) -> list[dict]:
        return self._call("GET", "/events", {"limit": limit})

    # ------------------------------------------------------------ waits
    def prepare(self, number: str) -> dict:
        return self._call("POST", f"/numbers/{urllib.parse.quote(number, safe='')}/prepare")

    def wait(self, number: str, since_id: int | None = None, timeout: float = 45,
             sender: str | None = None, contains: str | None = None) -> dict:
        """One long-poll (≤ timeout seconds). status: received | pending | switch_failed."""
        return self._call("POST", "/wait", body={
            "number": number, "since_id": since_id, "timeout": timeout, "sender": sender,
            "contains": contains}, timeout=timeout + 30)

    def wait_for_code(self, number: str, since_id: int | None = None, timeout: float = 600,
                      sender: str | None = None, contains: str | None = None) -> dict:
        """Long-poll repeatedly until a message arrives, the switch fails or timeout."""
        if since_id is None:
            since_id = self.prepare(number)["cursor"]
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            r = self.wait(number, since_id, min(45, max(1, left)), sender, contains)
            if r["status"] != "pending" or left <= 45:
                return r

    # ------------------------------------------------------------ manage
    def update_number(self, key: str, **fields) -> dict:
        return self._call("PATCH", f"/numbers/{urllib.parse.quote(key, safe='')}", body=fields,
                          timeout=90)

    def switch(self, key: str, hold_seconds: int | None = None) -> dict:
        return self._call("POST", f"/numbers/{urllib.parse.quote(key, safe='')}/switch",
                          body={"hold_seconds": hold_seconds})

    def refresh_card(self) -> dict:
        return self._call("POST", "/card/refresh", timeout=90)

    def delete_number(self, iccid: str) -> dict:
        return self._call("DELETE", f"/numbers/{iccid}", {"confirm": iccid}, timeout=90)

    def send(self, number: str, to: str, text: str) -> dict:
        return self._call("POST", "/send", body={"number": number, "to": to, "text": text},
                          timeout=120)
