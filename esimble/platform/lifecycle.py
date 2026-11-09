"""MCP-driven platform lifecycle.

* Every running MCP server process holds a *lease* file in data/mcp-leases/.
* When a tool call finds the platform down, the MCP server starts it with
  scripts/platform.sh and writes data/platform.autostart (the platform's PID).
* The platform watches the leases. If it was auto-started and no MCP process has
  been alive for `mcp_idle_stop_seconds`, it returns every group to its resident
  number and shuts itself down. A platform started by hand is never stopped.
"""

from __future__ import annotations

import atexit
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from ..config import DATA_DIR, ROOT

LEASE_DIR = DATA_DIR / "mcp-leases"
MARKER = DATA_DIR / "platform.autostart"
PIDFILE = DATA_DIR / "platform.pid"
START_LOCK = DATA_DIR / "platform.start.lock"
SCRIPT = ROOT / "scripts" / "platform.sh"


# ------------------------------------------------------------------ processes
# command-line markers of processes that may hold a lease
LEASE_MARKERS = ("esim-sms-mcp", "esim-platform", "esimble.platform")


def pid_alive(pid: int, must_contain: str | tuple = "") -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    if must_contain:   # guard against PID reuse
        try:
            cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True,
                                 text=True, timeout=3).stdout
        except (OSError, subprocess.TimeoutExpired):
            return True
        markers = (must_contain,) if isinstance(must_contain, str) else must_contain
        return any(m in cmd for m in markers)
    return True


# --------------------------------------------------------------------- leases
def _lease_path(pid: int | None = None) -> Path:
    return LEASE_DIR / f"{pid or os.getpid()}.json"


def register_lease(client: str = "") -> Path:
    LEASE_DIR.mkdir(parents=True, exist_ok=True)
    p = _lease_path()
    p.write_text(json.dumps({"pid": os.getpid(), "started": time.time(), "client": client}))
    atexit.register(remove_lease)
    return p


def remove_lease() -> None:
    try:
        _lease_path().unlink()
    except FileNotFoundError:
        pass


def live_leases() -> list[dict]:
    """Leases of MCP processes that are still running (stale ones are removed)."""
    out = []
    if not LEASE_DIR.exists():
        return out
    for f in LEASE_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text())
            pid = int(d["pid"])
        except (ValueError, KeyError, OSError):
            f.unlink(missing_ok=True)
            continue
        if pid_alive(pid, LEASE_MARKERS):
            out.append(d)
        else:
            f.unlink(missing_ok=True)
    return out


# --------------------------------------------------------------------- marker
def autostarted_pid() -> int | None:
    try:
        return int(MARKER.read_text().strip())
    except (OSError, ValueError):
        return None


def mark_autostarted(pid: int) -> None:
    MARKER.write_text(str(pid))


def clear_marker() -> None:
    MARKER.unlink(missing_ok=True)


# ---------------------------------------------------------------------- start
def start_platform(healthy, timeout: float = 120) -> bool:
    """Start the platform if `healthy()` is false. Returns True if this call started it.

    A file lock serialises concurrent MCP processes."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(START_LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if healthy():
            return False
        r = subprocess.run([str(SCRIPT), "start"], cwd=str(ROOT), stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=sys.stderr, timeout=timeout)
        if r.returncode != 0 or not healthy():
            raise RuntimeError(f"platform did not start (see {ROOT}/logs/platform-stdout.log)")
        try:
            mark_autostarted(int(PIDFILE.read_text().strip()))
        except (OSError, ValueError):
            pass
        return True
