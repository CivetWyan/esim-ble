"""ES10 (LPA <-> eUICC ISD-R) commands on top of an ApduTransport.

Follows GSMA SGP.22 and lpac euicc/es10*.c: STORE DATA 80 E2 P1 P2, P1=0x91
for the last block of a request, 0x11 otherwise, P2 = block number.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from . import tlv
from .transport import ApduTransport

log = logging.getLogger("es10")

ISD_R_AID = bytes.fromhex("A0000005591010FFFFFFFF8900000100")
DEFAULT_TAC = bytes.fromhex("35290611")
DEFAULT_MSS = 120


class CardError(Exception):
    def __init__(self, msg: str, sw: int | None = None):
        super().__init__(msg if sw is None else f"{msg} (SW={sw:04X})")
        self.sw = sw


def _sw(resp: bytes) -> int:
    return (resp[-2] << 8) | resp[-1] if len(resp) >= 2 else -1


def _sw_ok(sw: int) -> bool:
    # 9000, or 91xx (card has a proactive command queued for the handset)
    return sw >> 8 in (0x90, 0x91)


@dataclass
class Profile:
    iccid: str
    state: str
    name: str
    provider: str
    nickname: str
    profile_class: str

    def label(self) -> str:
        return self.nickname or self.name or self.provider or self.iccid


PROFILE_STATE = {0: "disabled", 1: "enabled"}
PROFILE_CLASS = {0: "test", 1: "provisioning", 2: "operational"}

BPP_COMMAND = {
    0: "initialiseSecureChannel", 1: "configureISDP", 2: "storeMetadata",
    3: "storeMetadata2", 4: "replaceSessionKeys", 5: "loadProfileElements",
}
BPP_ERROR_REASON = {
    1: "incorrectInputValues", 2: "invalidSignature", 3: "invalidTransactionId",
    4: "unsupportedCrtValues", 5: "unsupportedRemoteOperationType",
    6: "unsupportedProfileClass", 7: "scp03tStructureError", 8: "scp03tSecurityError",
    9: "installFailedDueToIccidAlreadyExistsOnEuicc",
    10: "installFailedDueToInsufficientMemoryForProfile",
    11: "installFailedDueToInterruption", 12: "installFailedDueToPEProcessingError",
    13: "installFailedDueToDataMismatch", 14: "testProfileInstallFailedDueToInvalidNaaKey",
    15: "pprNotAllowed", 127: "installFailedDueToUnknownError",
}


class Euicc:
    def __init__(self, transport: ApduTransport, mss: int = DEFAULT_MSS):
        self.t = transport
        self.mss = mss
        self.channel = 0

    # ------------------------------------------------------------ channel

    def _cla(self, cla: int) -> int:
        ch = self.channel
        if ch < 4:
            return (cla & 0xFC) | ch
        return (cla & 0xF0) | 0x40 | (ch - 4)

    async def _xfer(self, apdu: bytes) -> bytes:
        """Send one APDU; follow 61xx (GET RESPONSE) and 6Cxx (resend with Le)."""
        data = bytearray()
        cur = apdu
        for _ in range(64):
            resp = await self.t.transmit(cur)
            sw = _sw(resp)
            if sw >> 8 == 0x61:
                data += resp[:-2]
                cur = bytes([self._cla(0x00), 0xC0, 0x00, 0x00, sw & 0xFF])
                continue
            if sw >> 8 == 0x6C:
                cur = cur[:-1] + bytes([sw & 0xFF])
                continue
            data += resp
            return bytes(data)
        raise CardError("response reassembly did not finish")

    async def open_isdr(self, aid: bytes = ISD_R_AID) -> None:
        # Close stale channels 1..3 from earlier sessions (NekokoLPA2 default).
        for ch in (1, 2, 3):
            try:
                await self.t.transmit(bytes([0x00, 0x70, 0x80, ch, 0x00]))
            except Exception:  # noqa: BLE001 - best effort
                pass
        # TERMINAL CAPABILITY: announce local LPA / eUICC support.
        try:
            await self.t.transmit(bytes.fromhex("80AA00000AA9088100820101830107"))
        except Exception:  # noqa: BLE001
            pass
        self.channel = 0
        resp = await self.t.transmit(bytes.fromhex("0070000001"))
        if len(resp) >= 3 and _sw(resp) == 0x9000:
            self.channel = resp[0]
        else:
            log.warning("MANAGE CHANNEL failed (%s); using basic channel", resp.hex())
        sel = bytes([self._cla(0x00), 0xA4, 0x04, 0x00, len(aid)]) + aid
        resp = await self._xfer(sel)
        sw = _sw(resp)
        if not (_sw_ok(sw) or sw >> 8 == 0x9F):
            if sw == 0x6999 or sw == 0x6985:
                raise CardError("ISD-R temporarily unavailable; card is mid "
                                "profile state change, wait ~30s and retry", sw)
            raise CardError("SELECT ISD-R failed", sw)
        log.info("ISD-R selected on logical channel %d", self.channel)

    async def close_isdr(self) -> None:
        if self.channel:
            try:
                await self.t.transmit(bytes([0x00, 0x70, 0x80, self.channel, 0x00]))
            except Exception:  # noqa: BLE001
                pass
            self.channel = 0

    # ------------------------------------------------------------ ES10 core

    async def es10(self, request: bytes) -> bytes:
        """Send one ES10 request (DER), split into STORE DATA blocks."""
        out = bytearray()
        blocks = [request[i:i + self.mss] for i in range(0, len(request), self.mss)] or [b""]
        for seq, block in enumerate(blocks):
            last = seq == len(blocks) - 1
            apdu = bytes([self._cla(0x80), 0xE2, 0x91 if last else 0x11,
                          seq & 0xFF, len(block)]) + block
            resp = await self._xfer(apdu)
            sw = _sw(resp)
            if not _sw_ok(sw):
                raise CardError(f"STORE DATA block {seq} rejected", sw)
            out += resp[:-2]
        return bytes(out)

    # ------------------------------------------------------------ reads

    async def get_eid(self) -> str:
        resp = await self.es10(bytes.fromhex("BF3E035C015A"))
        return tlv.must(tlv.first(resp).value, 0x5A).value.hex().upper()

    async def get_euicc_info1(self) -> bytes:
        return await self.es10(bytes.fromhex("BF2000"))

    async def get_euicc_challenge(self) -> bytes:
        resp = await self.es10(bytes.fromhex("BF2E00"))
        return tlv.must(tlv.first(resp).value, 0x80).value

    async def free_nvm(self) -> int | None:
        """Free non-volatile memory in bytes, from EUICCInfo2.extCardResource."""
        resp = await self.es10(bytes.fromhex("BF2200"))
        ext = tlv.find(tlv.first(resp).value, 0x84)
        if ext is None:
            return None
        for t in tlv.children(ext.value):
            if t.tag == 0x82:
                return tlv.to_int(t.value)
        return None

    async def list_profiles(self) -> list[Profile]:
        resp = await self.es10(bytes.fromhex("BF2D00"))
        body = tlv.first(resp).value
        lst = tlv.find(body, 0xA0)
        if lst is None:
            err = tlv.find(body, 0x81)
            raise CardError(f"ProfileInfoList error {err.value.hex() if err else body.hex()}")
        profiles = []
        for e in tlv.children(lst.value):
            if e.tag != 0xE3:
                continue
            f = {t.tag: t.value for t in tlv.children(e.value)}
            profiles.append(Profile(
                iccid=tlv.decode_bcd_swapped(f.get(0x5A, b"")),
                state=PROFILE_STATE.get(tlv.to_int(f.get(0x9F70, b"")), "?"),
                name=f.get(0x92, b"").decode("utf-8", "replace"),
                provider=f.get(0x91, b"").decode("utf-8", "replace"),
                nickname=f.get(0x90, b"").decode("utf-8", "replace"),
                profile_class=PROFILE_CLASS.get(tlv.to_int(f.get(0x95, b"\x02")), "?"),
            ))
        return profiles

    # ------------------------------------------------------------ download

    async def authenticate_server(self, server_signed1: bytes, server_signature1: bytes,
                                  euicc_ci_pkid: bytes, server_certificate: bytes,
                                  matching_id: str | None, tac: bytes = DEFAULT_TAC) -> bytes:
        """ES10b.AuthenticateServer; inputs are the raw DER values from ES9+."""
        device_info = tlv.encode(0xA1, tlv.encode(0x80, tac) + tlv.encode(0xA1, b""))
        ctx = b""
        if matching_id:
            ctx += tlv.encode(0x80, matching_id.encode())
        ctx += device_info
        req = tlv.encode(0xBF38, server_signed1 + server_signature1 + euicc_ci_pkid
                         + server_certificate + tlv.encode(0xA0, ctx))
        resp = await self.es10(req)
        _check_choice(resp, "AuthenticateServer")
        return resp

    async def prepare_download(self, smdp_signed2: bytes, smdp_signature2: bytes,
                               smdp_certificate: bytes, hash_cc: bytes | None = None) -> bytes:
        """ES10b.PrepareDownload; hash_cc only when the SM-DP+ demands a code."""
        body = smdp_signed2 + smdp_signature2
        if hash_cc is not None:
            body += tlv.encode(0x04, hash_cc)
        body += smdp_certificate
        resp = await self.es10(tlv.encode(0xBF21, body))
        _check_choice(resp, "PrepareDownload")
        return resp

    async def cancel_session(self, transaction_id: bytes, reason: int = 0) -> bytes:
        """ES10b.CancelSession (reason 0 = endUserRejection, 127 = undefined)."""
        req = tlv.encode(0xBF41, tlv.encode(0x80, transaction_id)
                         + tlv.encode(0x81, bytes([reason])))
        return await self.es10(req)

    async def notification_sent(self, seq_number: int) -> int:
        """ES10b.RemoveNotificationFromList; returns deleteNotificationStatus."""
        resp = await self.es10(tlv.encode(0xBF30, tlv.encode(0x80, tlv.int_bytes(seq_number))))
        return tlv.to_int(tlv.must(tlv.first(resp).value, 0x80).value)

    async def load_bound_profile_package(self, bpp: bytes, progress=None) -> "InstallResult":
        """ES10b.LoadBoundProfilePackage, segmented per SGP.22 2.5.5 / lpac."""
        root = tlv.first(bpp)
        if root.tag != 0xBF36:
            raise CardError(f"not a BoundProfilePackage (tag {root.tag:X})")
        segments: list[bytes] = []
        init = tlv.must(root.value, 0xBF23)
        # BF36 header + InitialiseSecureChannel
        segments.append(root.header + init.raw)
        segments.append(tlv.must(root.value, 0xA0).raw)          # ConfigureISDP
        a1 = tlv.must(root.value, 0xA1)                           # StoreMetadata
        segments.append(a1.header)
        segments.extend(c.raw for c in tlv.children(a1.value))
        a2 = tlv.find(root.value, 0xA2)                           # ReplaceSessionKeys
        if a2 is not None:
            segments.append(a2.raw)
        a3 = tlv.must(root.value, 0xA3)                           # Profile elements
        segments.append(a3.header)
        segments.extend(c.raw for c in tlv.children(a3.value))

        total = sum(len(s) for s in segments)
        sent = 0
        for i, seg in enumerate(segments):
            resp = await self.es10(seg)
            sent += len(seg)
            if progress:
                progress(i + 1, len(segments), sent, total)
            if resp:
                result = InstallResult.parse(resp)
                if not result.ok or i == len(segments) - 1:
                    return result
        raise CardError("card returned no ProfileInstallationResult after the last segment")

    # ------------------------------------------------------------ management

    async def _profile_op(self, tag: int, iccid: str, extra: bytes = b"",
                          wrap: bool = True) -> int:
        ident = tlv.encode(0x5A, tlv.encode_bcd_swapped(iccid))
        body = (tlv.encode(0xA0, ident) if wrap else ident) + extra
        resp = await self.es10(tlv.encode(tag, body))
        return tlv.to_int(tlv.must(tlv.first(resp).value, 0x80).value)

    async def enable_profile(self, iccid: str, refresh: bool = True) -> int:
        return await self._profile_op(0xBF31, iccid, tlv.encode(0x81, b"\xff" if refresh else b"\x00"))

    async def disable_profile(self, iccid: str, refresh: bool = True) -> int:
        return await self._profile_op(0xBF32, iccid, tlv.encode(0x81, b"\xff" if refresh else b"\x00"))

    async def delete_profile(self, iccid: str) -> int:
        return await self._profile_op(0xBF33, iccid, wrap=False)

    async def set_nickname(self, iccid: str, nickname: str) -> int:
        return await self._profile_op(0xBF29, iccid, tlv.encode(0x90, nickname.encode()), wrap=False)


def _check_choice(resp: bytes, what: str) -> None:
    """Responses shaped CHOICE { ok [0], error [1] INTEGER }."""
    body = tlv.first(resp).value
    inner = tlv.first(body)
    if inner.tag == 0xA1 or inner.tag == 0x81:
        code = tlv.to_int(inner.value if inner.tag == 0x81 else
                          (tlv.find(inner.value, 0x02) or tlv.first(inner.value)).value)
        raise CardError(f"{what} refused by eUICC, error code {code}")


@dataclass
class InstallResult:
    raw: bytes            # whole BF37, sent back to the SM-DP+ as notification
    ok: bool
    seq_number: int | None
    iccid: str | None
    notification_address: str | None
    bpp_command: str | None = None
    error_reason: str | None = None

    @classmethod
    def parse(cls, resp: bytes) -> "InstallResult":
        root = tlv.first(resp)
        if root.tag != 0xBF37:
            raise CardError(f"unexpected install response tag {root.tag:X}")
        data = tlv.must(root.value, 0xBF27).value
        meta = tlv.must(data, 0xBF2F).value
        seq = tlv.find(meta, 0x80)
        iccid = tlv.find(meta, 0x5A)
        addr = tlv.find(meta, 0x0C)
        final = tlv.first(tlv.must(data, 0xA2).value)
        res = cls(raw=root.raw, ok=final.tag == 0xA0,
                  seq_number=tlv.to_int(seq.value) if seq else None,
                  iccid=tlv.decode_bcd_swapped(iccid.value) if iccid else None,
                  notification_address=addr.value.decode() if addr else None)
        if not res.ok:
            for t in tlv.children(final.value):
                if t.tag == 0x80:
                    res.bpp_command = BPP_COMMAND.get(tlv.to_int(t.value), str(tlv.to_int(t.value)))
                elif t.tag == 0x81:
                    res.error_reason = BPP_ERROR_REASON.get(tlv.to_int(t.value), str(tlv.to_int(t.value)))
        return res
