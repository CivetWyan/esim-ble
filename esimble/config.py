"""Platform configuration, stored in data/config.json (git-ignored, chmod 600)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("ESIMBLE_DATA", ROOT / "data"))
LOG_DIR = Path(os.environ.get("ESIMBLE_LOGS", ROOT / "logs"))
CONFIG_PATH = DATA_DIR / "config.json"


@dataclass
class Config:
    # 9eSIM card (BLE)
    ble_device: str = "9eSIM000000"
    ble_password: str | None = None          # None = device default
    insecure_tls: bool = True                # SM-DP+ certs chain to GSMA CI, not a public CA
    # MiFi modem (SMS receive/send)
    mifi_host: str = "192.168.0.1"
    mifi_password: str = ""

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False))
        os.chmod(tmp, 0o600)
        tmp.replace(CONFIG_PATH)

    def public(self) -> dict:
        d = asdict(self)
        for k in ("ble_password", "mifi_password"):
            d.pop(k, None)
        d["mifi_password_set"] = bool(self.mifi_password)
        return d



def load() -> Config:
    if CONFIG_PATH.exists():
        raw = json.loads(CONFIG_PATH.read_text())
        known = {k: v for k, v in raw.items() if k in Config.__dataclass_fields__}
        return Config(**known)
    return Config()
