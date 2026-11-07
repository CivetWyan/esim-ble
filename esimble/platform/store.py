"""Platform storage on top of esimble.db.Database (same SQLite file).

Adds a few columns to `messages` at startup (idempotent ALTER TABLE) and the
queries the platform needs. The base Database class is not modified.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..db import Database, now_iso

EXTRA_MESSAGE_COLUMNS = {
    "attribution": "TEXT DEFAULT ''",   # active | startup | sent
    "code": "TEXT DEFAULT ''",          # extracted verification code
    "mifi_deleted": "INTEGER DEFAULT 0",
}

_KW = r"(?:验证码|校验码|动态码|确认码|动态密码|激活码|code|Code|CODE|OTP|otp|PIN|pin)"
CODE_PATTERNS = [
    # keyword, a few separators, then an alphanumeric code containing a digit: "验证码：520cc"
    re.compile(_KW + r"[^0-9A-Za-z]{0,12}([0-9A-Za-z]{3,10})(?![0-9A-Za-z])"),
    # keyword, a few words, then a numeric code: "code is 7731"
    re.compile(_KW + r".{0,16}?(?<![0-9A-Za-z+])([0-9]{4,8})(?![0-9])"),
    # numeric code before a phrase: "552014 is your Google code"
    re.compile(r"(?<![0-9])([0-9]{4,8})[^0-9]{0,6}(?:是你的|为您的|是您的|is your|is the)"),
    # any standalone 4-8 digit number that is not a date / time / phone number
    re.compile(r"(?<![0-9+.:/-])([0-9]{4,8})(?![0-9年月日号点.:/-])"),
]


def extract_code(text: str) -> str:
    for p in CODE_PATTERNS:
        for m in p.finditer(text or ""):
            c = m.group(1)
            if any(ch.isdigit() for ch in c):
                return c
    return ""


def norm_iccid(s: str) -> str:
    return (s or "").strip().upper().rstrip("F")


def iccid_match(a: str, b: str) -> bool:
    """MiFi ziccid vs card ICCID. Tolerates F padding and a dropped Luhn digit."""
    a, b = norm_iccid(a), norm_iccid(b)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 18 and len(long_) - len(short) == 1 and long_.startswith(short)


class PlatformDB(Database):
    def __init__(self, path: Path):
        super().__init__(path)
        cols = {r["name"] for r in self.q("PRAGMA table_info(messages)")}
        for name, ddl in EXTRA_MESSAGE_COLUMNS.items():
            if name not in cols:
                self.x(f"ALTER TABLE messages ADD COLUMN {name} {ddl}")
        self.x("CREATE INDEX IF NOT EXISTS idx_messages_dir ON messages(direction, id)")
        # keep extracted codes in step with the current extraction rules
        for r in self.q("SELECT id, body, code FROM messages WHERE direction='in' "
                        "ORDER BY id DESC LIMIT 2000"):
            c = extract_code(r["body"])
            if c != r["code"]:
                self.x("UPDATE messages SET code=? WHERE id=?", (c, r["id"]))

    def profile_by_iccid(self, iccid: str) -> dict | None:
        for p in self.profiles(include_removed=True):
            if iccid_match(p["iccid"], iccid):
                return p
        return None

    def find_messages(self, iccid: str | None = None, direction: str | None = None,
                      since_id: int = 0, limit: int = 50, sender: str | None = None,
                      contains: str | None = None, before_id: int | None = None) -> list[dict]:
        where, args = ["id > ?"], [since_id]
        if iccid:
            where.append("iccid = ?")
            args.append(iccid)
        if direction:
            where.append("direction = ?")
            args.append(direction)
        if sender:
            where.append("peer LIKE ?")
            args.append(f"%{sender}%")
        if contains:
            where.append("body LIKE ?")
            args.append(f"%{contains}%")
        if before_id:
            where.append("id < ?")
            args.append(before_id)
        order = "ASC" if since_id else "DESC"
        return self.q(f"SELECT * FROM messages WHERE {' AND '.join(where)} "
                      f"ORDER BY id {order} LIMIT ?", (*args, max(1, min(limit, 500))))

    def counts(self) -> dict:
        rows = self.q("SELECT iccid, COUNT(*) AS n, MAX(id) AS last_id FROM messages "
                      "WHERE direction='in' GROUP BY iccid")
        return {r["iccid"]: r for r in rows}

    def store_modem_message(self, m: dict, profile: dict | None, attribution: str) -> int | None:
        """Insert one MiFi inbox item. Returns row id, or None when already stored."""
        iccid = profile["iccid"] if profile else ""
        key = f"mifi:{m['direction']}:{m['peer']}:{m['sms_time']}:{m['body']}"
        return self.add_message(
            iccid=iccid, msisdn=(profile or {}).get("msisdn", ""), direction=m["direction"],
            peer=m["peer"], body=m["body"], sms_time=m["sms_time"],
            status=m["status"], via="modem", mifi_id=m["mifi_id"], mifi_tag=m["tag"],
            dedup_key=key, attribution=attribution,
            code=extract_code(m["body"]) if m["direction"] == "in" else "")

    def dedup_exists(self, m: dict) -> bool:
        key = f"mifi:{m['direction']}:{m['peer']}:{m['sms_time']}:{m['body']}"
        return self.one("SELECT 1 FROM messages WHERE dedup_key=?", (key,)) is not None

    def mark_deleted(self, m: dict) -> None:
        key = f"mifi:{m['direction']}:{m['peer']}:{m['sms_time']}:{m['body']}"
        self.x("UPDATE messages SET mifi_deleted=1 WHERE dedup_key=?", (key,))

    def add_sent(self, iccid: str, msisdn: str, peer: str, body: str, status: str,
                 via: str) -> int:
        return self.add_message(iccid=iccid, msisdn=msisdn, direction="out", peer=peer, body=body,
                                sms_time=now_iso(), status=status, via=via,
                                dedup_key=f"send:{now_iso()}:{peer}:{id(body)}:{body}",
                                attribution="sent")

    def set_message_status(self, mid: int, status: str) -> None:
        self.x("UPDATE messages SET status=? WHERE id=?", (status, mid))

    def delete_message(self, mid: int) -> None:
        self.x("DELETE FROM messages WHERE id=?", (mid,))
