"""Minimal BER-TLV helpers for SGP.22 ES10 payloads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator


@dataclass
class Tlv:
    tag: int
    value: bytes
    raw: bytes  # full encoding: tag + length + value
    header_len: int

    @property
    def header(self) -> bytes:
        return self.raw[: self.header_len]


def read(buf: bytes, off: int = 0) -> tuple[Tlv, int]:
    start = off
    tag = buf[off]
    off += 1
    if tag & 0x1F == 0x1F:
        while True:
            b = buf[off]
            off += 1
            tag = (tag << 8) | b
            if not b & 0x80:
                break
    length = buf[off]
    off += 1
    if length & 0x80:
        n = length & 0x7F
        length = int.from_bytes(buf[off : off + n], "big")
        off += n
    end = off + length
    if end > len(buf):
        raise ValueError(f"TLV {tag:X} truncated: need {end}, have {len(buf)}")
    return Tlv(tag, buf[off:end], buf[start:end], off - start), end


def children(buf: bytes) -> Iterator[Tlv]:
    off = 0
    while off < len(buf):
        t, off = read(buf, off)
        yield t


def first(buf: bytes) -> Tlv:
    return read(buf, 0)[0]


def find(buf: bytes, tag: int) -> Tlv | None:
    for t in children(buf):
        if t.tag == tag:
            return t
    return None


def must(buf: bytes, tag: int) -> Tlv:
    t = find(buf, tag)
    if t is None:
        raise ValueError(f"tag {tag:X} not found")
    return t


def encode(tag: int, value: bytes) -> bytes:
    tag_bytes = tag.to_bytes(max(1, (tag.bit_length() + 7) // 8), "big")
    n = len(value)
    if n < 0x80:
        length = bytes([n])
    else:
        nb = n.to_bytes((n.bit_length() + 7) // 8, "big")
        length = bytes([0x80 | len(nb)]) + nb
    return tag_bytes + length + value


def to_int(value: bytes) -> int:
    return int.from_bytes(value, "big", signed=False) if value else 0


def int_bytes(n: int) -> bytes:
    """Minimal DER INTEGER content for a non-negative int."""
    b = n.to_bytes(max(1, (n.bit_length() + 8) // 8), "big")
    return b


def decode_bcd_swapped(b: bytes) -> str:
    """ICCID / EID style nibble-swapped BCD, trailing F stripped."""
    return "".join(f"{x & 0x0F:X}{x >> 4:X}" for x in b).rstrip("F")


def encode_bcd_swapped(s: str) -> bytes:
    if len(s) % 2:
        s += "F"
    return bytes(int(s[i + 1] + s[i], 16) for i in range(0, len(s), 2))
