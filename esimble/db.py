"""SQLite storage. One connection guarded by a lock; all calls are short."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
  iccid TEXT PRIMARY KEY,
  label TEXT DEFAULT '',            -- platform display name
  msisdn TEXT DEFAULT '',           -- phone number (manual or learned from MiFi)
  notes TEXT DEFAULT '',
  tags TEXT DEFAULT '',
  poll_enabled INTEGER DEFAULT 1,
  priority INTEGER DEFAULT 0,
  card_provider TEXT DEFAULT '',
  card_name TEXT DEFAULT '',
  card_nickname TEXT DEFAULT '',
  card_state TEXT DEFAULT '',
  profile_class TEXT DEFAULT '',
  on_card INTEGER DEFAULT 1,
  imsi TEXT DEFAULT '',
  last_active_at TEXT,
  last_switch_error TEXT DEFAULT '',
  last_switch_error_at REAL DEFAULT 0,
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  iccid TEXT, msisdn TEXT DEFAULT '',
  direction TEXT NOT NULL,          -- in | out
  peer TEXT DEFAULT '',             -- sender (in) / recipient (out)
  body TEXT DEFAULT '',
  sms_time TEXT DEFAULT '',         -- timestamp reported by the modem
  received_at TEXT,                 -- when the platform stored it
  status TEXT DEFAULT '',           -- out: pending | accepted | failed
  via TEXT DEFAULT '',              -- out: web | rest | sdk | mcp
  mifi_id TEXT DEFAULT '', mifi_tag TEXT DEFAULT '',
  dedup_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_messages_iccid ON messages(iccid, id);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, level TEXT, kind TEXT, message TEXT
);
"""

def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)

    def q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args: tuple = ()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, args)
            return cur.lastrowid if cur.lastrowid else cur.rowcount

    # ---------------------------------------------------------- events
    def event(self, kind: str, message: str, level: str = "info") -> None:
        self.x("INSERT INTO events(ts, level, kind, message) VALUES (?,?,?,?)",
               (now_iso(), level, kind, message))
        self.x("DELETE FROM events WHERE id < (SELECT MAX(id) - 2000 FROM events)")

    def events(self, limit: int = 100) -> list[dict]:
        return self.q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    # ---------------------------------------------------------- settings
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        self.x("INSERT INTO settings(key, value) VALUES (?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    # ---------------------------------------------------------- profiles
    def profiles(self, include_removed: bool = False) -> list[dict]:
        sql = "SELECT * FROM profiles" + ("" if include_removed else " WHERE on_card=1")
        return self.q(sql + " ORDER BY priority DESC, created_at")

    def profile(self, key: str) -> dict | None:
        """Look a number up by ICCID, phone number or label."""
        k = key.strip()
        digits = k.lstrip("+")
        return (self.one("SELECT * FROM profiles WHERE iccid=?", (k,))
                or self.one("SELECT * FROM profiles WHERE msisdn<>'' AND "
                            "(msisdn=? OR ltrim(msisdn,'+')=?)", (k, digits))
                or self.one("SELECT * FROM profiles WHERE label<>'' AND label=?", (k,)))

    def sync_card_profiles(self, card_profiles: list) -> None:
        """Merge the card's ES10 profile list into the table."""
        seen = set()
        ts = now_iso()
        for p in card_profiles:
            seen.add(p.iccid)
            if self.one("SELECT 1 FROM profiles WHERE iccid=?", (p.iccid,)):
                self.x("UPDATE profiles SET card_provider=?, card_name=?, card_nickname=?, "
                       "card_state=?, profile_class=?, on_card=1, updated_at=? WHERE iccid=?",
                       (p.provider, p.name, p.nickname, p.state, p.profile_class, ts, p.iccid))
            else:
                self.x("INSERT INTO profiles(iccid, label, card_provider, card_name, card_nickname, "
                       "card_state, profile_class, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                       (p.iccid, p.nickname or p.name, p.provider, p.name, p.nickname, p.state,
                        p.profile_class, ts, ts))
        for row in self.q("SELECT iccid FROM profiles WHERE on_card=1"):
            if row["iccid"] not in seen:
                self.x("UPDATE profiles SET on_card=0, card_state='removed', updated_at=? "
                       "WHERE iccid=?", (ts, row["iccid"]))

    def update_profile(self, iccid: str, **fields) -> None:
        allowed = {"label", "msisdn", "notes", "tags", "poll_enabled", "priority", "imsi",
                   "last_active_at", "last_switch_error", "last_switch_error_at", "card_state",
                   "card_nickname", "on_card"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        sets["updated_at"] = now_iso()
        cols = ", ".join(f"{k}=?" for k in sets)
        self.x(f"UPDATE profiles SET {cols} WHERE iccid=?", (*sets.values(), iccid))

    # ---------------------------------------------------------- messages
    def add_message(self, **m) -> int | None:
        m.setdefault("received_at", now_iso())
        cols = ", ".join(m)
        try:
            return self.x(f"INSERT INTO messages({cols}) VALUES ({','.join('?' * len(m))})",
                          tuple(m.values()))
        except sqlite3.IntegrityError:
            return None  # duplicate

    def messages(self, iccid: str | None = None, direction: str | None = None,
                 since_id: int = 0, limit: int = 50) -> list[dict]:
        where, args = ["id > ?"], [since_id]
        if iccid:
            where.append("iccid = ?")
            args.append(iccid)
        if direction:
            where.append("direction = ?")
            args.append(direction)
        order = "ASC" if since_id else "DESC"
        return self.q(f"SELECT * FROM messages WHERE {' AND '.join(where)} "
                      f"ORDER BY id {order} LIMIT ?", (*args, limit))

    def max_message_id(self) -> int:
        row = self.one("SELECT MAX(id) AS m FROM messages")
        return (row or {}).get("m") or 0
