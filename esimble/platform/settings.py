"""Platform settings, stored in data/platform.json (chmod 600), separate from config.json."""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..config import DATA_DIR

PLATFORM_PATH = DATA_DIR / "platform.json"


@dataclass
class PlatformSettings:
    host: str = "127.0.0.1"
    port: int = 8787
    api_token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    # scheduling
    home_iccid: str = ""                  # resident number: kept online when nobody waits
    rotation_enabled: bool = False        # rotate through all numbers (messages may be lost)
    dwell_seconds: int = 600              # how long each number stays online in rotation
    poll_seconds: int = 5                 # inbox poll interval while online
    switch_timeout_seconds: int = 600     # enable + reboot + register + sms ready
    wait_hold_seconds: int = 600          # how long an on-demand wait keeps its number online
    switch_retry_after_seconds: int = 1800  # skip a number in rotation after a failed switch
    groups: list = field(default_factory=list)   # device groups, see devices.GroupConfig
    # inbox handling
    delete_after_store: bool = True
    # API permissions
    api_allow_send: bool = False          # REST/SDK/MCP send (admin web always may send)

    def save(self, path: Path = PLATFORM_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False))
        os.chmod(tmp, 0o600)
        tmp.replace(path)

    def public(self) -> dict:
        d = asdict(self)
        d.pop("api_token")
        d.pop("groups")
        return d

    EDITABLE = ("home_iccid", "rotation_enabled", "dwell_seconds", "poll_seconds", "switch_timeout_seconds",
                "wait_hold_seconds", "switch_retry_after_seconds", "delete_after_store",
                "api_allow_send")

    def update(self, values: dict) -> None:
        for k, v in values.items():
            if k not in self.EDITABLE:
                continue
            cur = getattr(self, k)
            if isinstance(cur, str):
                v = str(v or "")
            elif isinstance(cur, bool):
                v = bool(v)
            elif isinstance(cur, int):
                v = max(1, int(v))
            setattr(self, k, v)


def load(path: Path = PLATFORM_PATH) -> PlatformSettings:
    if path.exists():
        raw = json.loads(path.read_text())
        s = PlatformSettings(**{k: v for k, v in raw.items()
                                if k in PlatformSettings.__dataclass_fields__})
        if "home_iccid" not in raw:
            s.rotation_enabled = False   # v2 default: resident number, no rotation
    else:
        s = PlatformSettings()
    s.save(path)  # persist the generated token / new defaults
    return s
