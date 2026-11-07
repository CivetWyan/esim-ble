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
from esimble.platform.engine import Engine  # noqa: E402
from esimble.platform.settings import PlatformSettings  # noqa: E402
from esimble.platform.store import PlatformDB  # noqa: E402
from test_platform import A, B, FakeCard, FakeMifi  # noqa: E402

SAMPLES = [
    ("10690000", "【示例商城】您的验证码是 482913，5 分钟内有效，请勿泄露。"),
    ("+12025550123", "Your verification code is 7731. It expires in 10 minutes."),
    ("106575000", "【示例银行】动态码 520cc，用于登录验证。"),
    ("G-Verify", "G-552014 is your verification code."),
]


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="esim-demo-"))
    db = PlatformDB(tmp / "demo.db")
    s = PlatformSettings(api_token="demo", port=8788, home_iccid=A)
    s.save = lambda *a: None
    mifi = FakeMifi()
    card = FakeCard(db, mifi)
    eng = Engine(Config(), s, db, mifi=mifi, card=card)
    await eng.refresh_card()
    db.update_profile(A, label="Saily US", tags="us,otp", notes="常驻号")
    db.update_profile(B, label="Cellfie GE", msisdn="+995500000002", tags="ge")
    for peer, body in SAMPLES:
        mifi.deliver(peer, body)
        await eng._poll()
    app = create_app(eng)
    print("demo: http://127.0.0.1:8788/login?token=demo")
    await uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8788, log_level="warning")).serve()


if __name__ == "__main__":
    asyncio.run(main())
