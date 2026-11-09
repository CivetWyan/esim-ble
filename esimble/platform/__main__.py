"""esim-ble SMS platform.

  python -m esimble.platform serve          run admin web + REST + scheduler
  python -m esimble.platform token [--rotate]
  python -m esimble.platform mcp            MCP server on stdio (needs `serve` running)
  python -m esimble.platform mcp-config     print config snippets for Claude / Cursor / Codex
"""

from __future__ import annotations

import argparse
import json
import logging
import secrets
import sys
from logging.handlers import RotatingFileHandler

from ..config import DATA_DIR, LOG_DIR, ROOT
from . import settings as settings_mod


def serve(args) -> int:
    import uvicorn

    from .. import config as config_mod
    from .api import create_app
    from .manager import Manager
    from .store import PlatformDB

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s", "%m-%d %H:%M:%S")
    fh = RotatingFileHandler(LOG_DIR / "platform.log", maxBytes=5_000_000, backupCount=3)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root = logging.getLogger()
    root.handlers[:] = [fh, sh]
    root.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    for noisy in ("bleak", "asyncio", "uvicorn.access", "httpx"):
        logging.getLogger(noisy).setLevel(logging.DEBUG if args.verbose else logging.WARNING)

    cfg = config_mod.load()
    s = settings_mod.load()
    if args.port:
        s.port = args.port
    db = PlatformDB(DATA_DIR / "esimble.db")
    app = create_app(Manager(cfg, s, db))
    url = f"http://127.0.0.1:{s.port}"
    logging.getLogger("platform").info("admin: %s/login?token=%s", url, s.api_token)
    print(f"\n  管理后台: {url}/login?token={s.api_token}\n  API 文档: {url}/api/docs\n")
    uvicorn.run(app, host=s.host, port=s.port, log_config=None, access_log=False)
    return 0


def mcp_config() -> int:
    s = settings_mod.load()
    exe = ROOT / ".venv" / "bin" / "esim-sms-mcp"
    if not exe.exists():
        print("# .venv/bin/esim-sms-mcp is missing: run ./start.sh once (or "
              ".venv/bin/pip install -e .) first\n", file=sys.stderr)
    exe = str(exe)
    entry = {"command": exe, "args": [],
             "env": {"ESIM_PLATFORM_URL": f"http://127.0.0.1:{s.port}",
                     "ESIM_PLATFORM_TOKEN": s.api_token}}
    print("# Claude Code:")
    print(f"claude mcp add esim-sms -s user -e ESIM_PLATFORM_URL=http://127.0.0.1:{s.port} "
          f"-e ESIM_PLATFORM_TOKEN={s.api_token} -- {exe}\n")
    print("# Cursor (~/.cursor/mcp.json) / Claude Desktop:")
    print(json.dumps({"mcpServers": {"esim-sms": entry}}, indent=2) + "\n")
    print("# Codex (~/.codex/config.toml):")
    print("[mcp_servers.esim-sms]")
    print(f'command = "{exe}"')
    print("tool_timeout_sec = 120")
    print(f'env = {{ ESIM_PLATFORM_URL = "http://127.0.0.1:{s.port}", '
          f'ESIM_PLATFORM_TOKEN = "{s.api_token}" }}')
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m esimble.platform", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve")
    sv.add_argument("--port", type=int)
    sv.add_argument("-v", "--verbose", action="store_true")
    t = sub.add_parser("token")
    t.add_argument("--rotate", action="store_true")
    sub.add_parser("mcp")
    sub.add_parser("mcp-config")
    args = ap.parse_args(argv)
    if args.cmd == "serve":
        return serve(args)
    if args.cmd == "token":
        s = settings_mod.load()
        if args.rotate:
            s.api_token = secrets.token_urlsafe(24)
            s.save()
            print("token rotated; restart `serve` and update MCP configs", file=sys.stderr)
        print(s.api_token)
        print(f"http://127.0.0.1:{s.port}/login?token={s.api_token}", file=sys.stderr)
        return 0
    if args.cmd == "mcp":
        from .mcp_server import main as mcp_main
        mcp_main()
        return 0
    if args.cmd == "mcp-config":
        return mcp_config()
    return 1


if __name__ == "__main__":
    sys.exit(main())
