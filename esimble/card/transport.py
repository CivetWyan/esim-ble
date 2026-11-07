"""Transport interface the upper layers depend on.

The upper layers (ES10 card commands, ES9+ SM-DP+ client, CLI) only need a
raw APDU pipe to the eUICC. Implement `ApduTransport` for the 9eSIM BLE link
(see PROTOCOL.md sections 4-5 and NekokoLPA2 nble_sim_adapter.dart) and
register it in `esim9/cli.py:make_transport`.

Contract:
  * `open()`     connect + log in; leave the card in eUICC mode.
  * `transmit()` send one complete APDU (CLA already carries the logical
                 channel), return response data + SW1 SW2. No 61xx/6Cxx
                 handling needed here; the channel layer does that.
  * `close()`    release the link so the card advertises again.
"""

from __future__ import annotations

from typing import Protocol


class ApduTransport(Protocol):
    async def open(self) -> None: ...

    async def transmit(self, apdu: bytes) -> bytes: ...

    async def close(self) -> None: ...


class ReplayTransport:
    """Test double: answers APDUs from a scripted list of (apdu_prefix, response)."""

    def __init__(self, script: list[tuple[bytes, bytes]]):
        self.script = list(script)
        self.sent: list[bytes] = []

    async def open(self) -> None:
        pass

    async def transmit(self, apdu: bytes) -> bytes:
        self.sent.append(apdu)
        if not self.script:
            raise AssertionError(f"unexpected APDU {apdu.hex()}")
        prefix, resp = self.script.pop(0)
        if not apdu.startswith(prefix):
            raise AssertionError(f"expected {prefix.hex()}..., got {apdu.hex()}")
        return resp

    async def close(self) -> None:
        pass
