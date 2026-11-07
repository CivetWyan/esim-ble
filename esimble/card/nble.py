"""9eSIM NBLE-SIM BLE transport.

The wire format and authentication below mirror NekokoLPA2's native NBLE
transport.  The class deliberately exposes only the raw APDU transport used by
the ES10 layer; profile management belongs to the callers in this package.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable

from bleak import BleakClient, BleakScanner

try:  # TripleDES moved to ``decrepit`` in newer cryptography releases.
    from cryptography.hazmat.decrepit.ciphers import algorithms as _cipher_algorithms
except ImportError:  # pragma: no cover - for older installed cryptography
    from cryptography.hazmat.primitives.ciphers import algorithms as _cipher_algorithms
from cryptography.hazmat.primitives.ciphers import Cipher, modes


UART_SERVICE = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
UART_NOTIFY = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"
UART_WRITE = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"

_FRAME_SIZE = 101
_FRAME_DELAY = 0.008
_RESPONSE_TIMEOUT = 30.0
_HANDSHAKE_TIMEOUT = 5.0
_log = logging.getLogger(__name__)
_apdu_log = logging.getLogger("apdu")


class _Disconnected:
    pass


def default_password_for_device_name(device_name: str) -> str:
    """Return Nekoko's default password for a BLE display name."""

    clean = device_name.strip().upper()
    suffix = clean if len(clean) <= 6 else clean[-6:]
    return suffix[::-1]


def calculate_password_hex(value: str) -> str:
    """Reduce a management password to the six hex digits used by NBLE auth."""

    stripped = value.strip()
    if len(stripped) == 6 and all(c in "0123456789abcdefABCDEF" for c in stripped):
        return stripped.upper()

    crc = 0xB704CE
    for byte in value.encode():
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
    return f"{crc & 0xFFFFFF:06X}"


def _auth_key(device_name: str, password: str) -> bytes:
    suffix = device_name.strip().upper()[-6:][-2:]
    base = suffix + calculate_password_hex(password)
    return bytes.fromhex(base + base + base + "00000000")


def _triple_des_ecb(data: bytes, key: bytes, *, decrypt: bool) -> bytes:
    # PointyCastle accepts the 16-byte two-key form; expand K1,K2 to K1,K2,K1
    # for cryptography's 24-byte implementation without changing the cipher.
    cipher_key = key if len(key) == 24 else key + key[:8]
    cipher = Cipher(_cipher_algorithms.TripleDES(cipher_key), modes.ECB())
    context = cipher.decryptor() if decrypt else cipher.encryptor()
    return context.update(data) + context.finalize()


def _sum_check(payload: bytes) -> int:
    return sum(payload) & 0xFFFF


def encode_frames(counter: int, apdu: bytes, max_write_size: int = _FRAME_SIZE) -> list[bytes]:
    """Encode one APDU into NBLE protocol frames."""

    if max_write_size < 2:
        raise ValueError("max_write_size must leave room for a frame header")
    body = bytes((counter & 0xFF,)) + bytes(apdu)
    full = body + _sum_check(body).to_bytes(2, "big")
    capacity = max_write_size - 1
    frames: list[bytes] = []
    for offset in range(0, len(full), capacity):
        chunk = full[offset : offset + capacity]
        last = offset + len(chunk) == len(full)
        header = (0x20 if last else 0) | (len(frames) & 0x1F)
        frames.append(bytes((header,)) + chunk)
    return frames


def decode_frames(frames: Iterable[bytes], expected_counter: int | None = None) -> bytes:
    """Validate and decode a complete response frame sequence."""

    frame_list = list(frames)
    if not frame_list:
        raise ValueError("NBLE response is empty")
    payload = bytearray()
    for index, frame in enumerate(frame_list):
        if len(frame) <= 1:
            raise ValueError("NBLE response frame is truncated")
        header = frame[0]
        if header & 0xC0 or (header & 0x1F) != (index & 0x1F):
            raise ValueError("NBLE response frame sequence mismatch")
        if bool(header & 0x20) != (index == len(frame_list) - 1):
            raise ValueError("NBLE response final-frame marker mismatch")
        payload.extend(frame[1:])

    if len(payload) < 5:  # counter + at least SW1/SW2 + checksum
        raise ValueError("NBLE response payload is too short")
    raw = bytes(payload[:-2])
    checksum = int.from_bytes(payload[-2:], "big")
    expected_sum = _sum_check(raw)
    if checksum != expected_sum:
        raise ValueError(
            f"NBLE response checksum mismatch: got {checksum:04x}, "
            f"expected {expected_sum:04x}"
        )
    if expected_counter is not None and raw[0] != (expected_counter & 0xFF):
        raise ValueError(
            f"NBLE response counter mismatch: expected {expected_counter & 0xFF}, "
            f"received {raw[0]}"
        )
    apdu = raw[1:]
    if len(apdu) < 2:
        raise ValueError("NBLE response APDU is too short")
    return apdu


class NbleTransport:
    # Keep the protocol primitives available for offline callers and vectors.
    default_password_for_device_name = staticmethod(default_password_for_device_name)
    calculate_password_hex = staticmethod(calculate_password_hex)
    encode_frames = staticmethod(encode_frames)
    decode_frames = staticmethod(decode_frames)

    def __init__(self, device_name: str, password: str | None = None):
        self.device_name = device_name
        self.password = password if password is not None else default_password_for_device_name(device_name)
        self._client: Any | None = None
        self._write_char: Any | None = None
        self._notify_char: Any | None = None
        self._rx: asyncio.Queue[bytes | _Disconnected] = asyncio.Queue()
        self._counter = 0
        self._opened = False
        self._disconnect_error: RuntimeError | None = None

    @property
    def client(self) -> Any | None:
        return self._client

    async def open(self) -> None:
        """Scan, connect, authenticate, and leave the writer in eUICC mode."""

        await self.close()
        for attempt in range(3):
            try:
                await self._connect_once()
                await self._perform_handshake()
                self._counter = 0
                mode_response = await self._transmit_internal(bytes.fromhex("FE0D000001"))
                if len(mode_response) < 3 or mode_response[-2:] != b"\x90\x00":
                    raise RuntimeError(f"get mode failed: {mode_response.hex()}")
                mode = mode_response[0]
                if mode == 2:
                    self._opened = True
                    return
                if mode != 1:
                    raise RuntimeError(f"unknown 9eSIM mode {mode}")

                # SIM mode must be switched and the writer rebooted before the
                # eUICC APDU interface is usable again.
                await self._transmit_internal(bytes.fromhex("FE0C020001"))
            except Exception as exc:
                await self.close()
                if attempt == 2 or "wrong password" in str(exc).lower():
                    raise
                await asyncio.sleep(0.5)
                continue
            await self.close()
            await asyncio.sleep(4.0)
        raise RuntimeError("could not connect to 9eSIM in eUICC mode")

    async def transmit(self, apdu: bytes) -> bytes:
        if not self._opened:
            raise RuntimeError("NBLE transport is not open")
        response = await self._transmit_internal(bytes(apdu))
        _apdu_log.debug("<< %s", response.hex())
        return response

    async def close(self) -> None:
        """Stop notifications and disconnect so the writer advertises again."""

        self._opened = False
        client, notify = self._client, self._notify_char
        self._client = None
        self._write_char = None
        self._notify_char = None
        self._disconnect_error = None
        while not self._rx.empty():
            self._rx.get_nowait()
        if client is None:
            return
        if notify is not None:
            try:
                await client.stop_notify(UART_NOTIFY)
            except Exception:
                _log.debug("stopping NBLE notifications failed", exc_info=True)
        try:
            await client.disconnect()
        except Exception:
            _log.debug("disconnecting NBLE device failed", exc_info=True)

    async def _connect_once(self) -> None:
        devices = await BleakScanner.discover(timeout=5.0)
        device = next((d for d in devices if getattr(d, "name", None) == self.device_name), None)
        if device is None:
            raise RuntimeError(f"BLE device {self.device_name!r} not found")

        loop = asyncio.get_running_loop()

        def disconnected(_client: Any) -> None:
            if self._client is not _client:
                return  # a link we already released; don't poison the next one
            self._disconnect_error = RuntimeError("9eSIM BLE device disconnected")
            loop.call_soon_threadsafe(self._rx.put_nowait, _Disconnected())

        client = BleakClient(device, disconnected_callback=disconnected)
        await client.connect()
        self._client = client
        services = client.services
        get_services = getattr(client, "get_services", None)
        if get_services is not None:
            services = await get_services()
        service = services.get_service(UART_SERVICE) if hasattr(services, "get_service") else None
        if service is None:
            for candidate in services:
                if str(getattr(candidate, "uuid", "")).lower() == UART_SERVICE.lower():
                    service = candidate
                    break
        if service is None:
            raise RuntimeError("9eSIM UART service not found")
        chars = list(getattr(service, "characteristics", ()))
        self._write_char = next((c for c in chars if str(c.uuid).lower() == UART_WRITE.lower()), None)
        self._notify_char = next((c for c in chars if str(c.uuid).lower() == UART_NOTIFY.lower()), None)
        if self._write_char is None or self._notify_char is None:
            raise RuntimeError("9eSIM UART characteristics not found")

        while not self._rx.empty():
            self._rx.get_nowait()

        def on_notify(_sender: Any, data: bytearray | bytes) -> None:
            self._rx.put_nowait(bytes(data))

        await client.start_notify(UART_NOTIFY, on_notify)

    async def _perform_handshake(self) -> None:
        challenge = await self._wait_raw(
            lambda packet: len(packet) >= 17 and packet[0] == 0xE0,
            _HANDSHAKE_TIMEOUT,
            "authentication challenge",
        )
        plain = bytearray(_triple_des_ecb(challenge[1:17], _auth_key(self.device_name, self.password), decrypt=True))
        counter = ((plain[8] << 8) | plain[7]) + 1
        counter &= 0xFFFF
        plain[7] = counter & 0xFF
        plain[8] = (counter >> 8) & 0xFF
        await self._write_raw(b"\xE0" + _triple_des_ecb(bytes(plain), _auth_key(self.device_name, self.password), decrypt=False))
        ack = await self._wait_raw(
            lambda packet: len(packet) >= 3 and packet[0] == 0xE0 and packet[1] in (0x90, 0x6A),
            _HANDSHAKE_TIMEOUT,
            "authentication acknowledgement",
        )
        status = bytes(ack[1:3])
        if status == b"\x6A\x86":
            raise RuntimeError("9eSIM authentication failed: wrong password")
        if status != b"\x90\x00":
            raise RuntimeError(f"9eSIM authentication failed: {status.hex()}")

    async def _transmit_internal(self, apdu: bytes) -> bytes:
        counter = self._counter
        self._counter = (self._counter + 1) & 0xFF
        while not self._rx.empty():
            self._rx.get_nowait()
        _apdu_log.debug(">> %s", apdu.hex())
        frames = encode_frames(counter, apdu)
        for frame in frames:
            await self._write_raw(frame)
            await asyncio.sleep(_FRAME_DELAY)

        response_frames = await self._wait_response_frames(counter, _RESPONSE_TIMEOUT)
        return decode_frames(response_frames, expected_counter=counter)

    async def _write_raw(self, data: bytes) -> None:
        if self._client is None or self._write_char is None:
            raise RuntimeError("9eSIM BLE transport is not connected")
        await self._client.write_gatt_char(self._write_char, data, response=False)

    async def _wait_raw(self, predicate: Any, timeout: float, description: str) -> bytes:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"timed out waiting for {description}")
            try:
                packet = await asyncio.wait_for(self._rx.get(), remaining)
            except asyncio.TimeoutError as exc:
                raise TimeoutError(f"timed out waiting for {description}") from exc
            if isinstance(packet, _Disconnected):
                raise self._disconnect_error or RuntimeError("9eSIM BLE device disconnected")
            if predicate(packet):
                return packet

    async def _wait_response_frames(self, counter: int, timeout: float) -> list[bytes]:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            first = await self._wait_raw(lambda _packet: True, max(0.0, deadline - asyncio.get_running_loop().time()), "APDU response")
            if len(first) <= 1 or first[0] & 0xC0 or first[0] & 0x1F:
                raise ValueError("NBLE response did not start with frame sequence 0")
            if len(first) < 2:
                raise ValueError("NBLE response frame is truncated")
            if first[1] != (counter & 0xFF):
                # Drain an old response, including all of its continuation frames.
                stale = first
                while not (stale[0] & 0x20):
                    stale = await self._wait_raw(lambda _packet: True, max(0.0, deadline - asyncio.get_running_loop().time()), "stale APDU response")
                continue

            frames = [first]
            expected_sequence = 1
            while not (frames[-1][0] & 0x20):
                frame = await self._wait_raw(lambda _packet: True, max(0.0, deadline - asyncio.get_running_loop().time()), "APDU response continuation")
                if len(frame) <= 1 or frame[0] & 0xC0 or (frame[0] & 0x1F) != expected_sequence:
                    raise ValueError(f"NBLE response frame sequence mismatch: expected {expected_sequence}")
                frames.append(frame)
                expected_sequence = (expected_sequence + 1) & 0x1F
            return frames


__all__ = [
    "NbleTransport",
    "UART_SERVICE",
    "UART_NOTIFY",
    "UART_WRITE",
    "calculate_password_hex",
    "default_password_for_device_name",
    "decode_frames",
    "encode_frames",
]
