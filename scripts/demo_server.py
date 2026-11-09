"""Demo: run the admin UI on fake hardware (no card, no MiFi) with made-up data.

    .venv/bin/python scripts/demo_server.py      # http://127.0.0.1:8788/login?token=demo

Used for the README screenshots. Nothing touches the real card, MiFi or data/.
"""

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import uvicorn  # noqa: E402

from esimble.config import Config  # noqa: E402
from esimble.platform.api import create_app  # noqa: E402
from esimble.platform.settings import PlatformSettings  # noqa: E402
from esimble.platform.store import PlatformDB  # noqa: E402
from test_platform import A, B  # noqa: E402

SAMPLES = [
    ("10690000", "【示例商城】您的验证码是 482913，5 分钟内有效，请勿泄露。"),
    ("+12025550123", "Your verification code is 7731. It expires in 10 minutes."),
    ("106575000", "【示例银行】动态码 520cc，用于登录验证。"),
    ("G-Verify", "G-552014 is your verification code."),
]


async def main() -> None:
    from test_devices import C, FakeManager
    from esimble.platform.devices import GroupConfig
    tmp = Path(tempfile.mkdtemp(prefix="esim-demo-"))
    db = PlatformDB(tmp / "demo.db")
    groups = [GroupConfig(id="g1", name="设备组 1", ble_device="9eSIM000000", home_iccid=A,
                          mifi_imei="860000000000001").to_dict(),
              GroupConfig(id="g2", name="设备组 2", ble_device="9eSIM111111", home_iccid=C,
                          mifi_host="192.168.1.1", mifi_imei="860000000000002").to_dict()]
    s = PlatformSettings(api_token="demo", port=8788, groups=groups)
    s.save = lambda *a: None
    mgr = FakeManager(Config(), s, db)
    mgr.start()
    await asyncio.sleep(0.5)
    db.update_profile(A, label="Saily US", tags="us,otp", msisdn="+15550000001")
    db.update_profile(B, label="Cellfie GE", msisdn="+995500000002", tags="ge")
    db.update_profile(C, label="CMCC 示例", msisdn="+8613800000003", tags="cn")
    e1, e2 = mgr.engines["g1"], mgr.engines["g2"]
    for i, (peer, body) in enumerate(SAMPLES):
        e = e1 if i % 2 == 0 else e2
        e.mifi.deliver(peer, body)
        await e._poll()
    app = create_app(mgr, manage_engine=False)
    print("demo: http://127.0.0.1:8788/login?token=demo")
    await uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8788, log_level="warning")).serve()

if __name__ == "__main__":
    asyncio.run(main())
