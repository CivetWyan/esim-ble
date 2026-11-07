"""One-shot profile download: activation code -> installed profile."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from . import tlv
from .es9p import Es9pClient, SmdpError, b64d
from .euicc import CardError, Euicc, InstallResult

log = logging.getLogger("download")


@dataclass
class ActivationCode:
    smdp: str
    matching_id: str
    oid: str | None = None
    cc_required: bool = False

    @classmethod
    def parse(cls, text: str) -> "ActivationCode":
        s = text.strip()
        if s.upper().startswith("LPA:"):
            s = s[4:]
        parts = s.split("$")
        if len(parts) < 2 or parts[0] != "1":
            raise ValueError(f"not an SGP.22 activation code: {text!r}")
        return cls(smdp=parts[1], matching_id=parts[2] if len(parts) > 2 else "",
                   oid=parts[3] if len(parts) > 3 and parts[3] else None,
                   cc_required=len(parts) > 4 and parts[4] == "1")


@dataclass
class ProfileMetadata:
    iccid: str | None
    provider: str | None
    name: str | None

    @classmethod
    def parse(cls, der: bytes) -> "ProfileMetadata":
        body = tlv.first(der).value  # BF25
        f = {t.tag: t.value for t in tlv.children(body)}
        return cls(
            iccid=tlv.decode_bcd_swapped(f[0x5A]) if 0x5A in f else None,
            provider=f[0x91].decode("utf-8", "replace") if 0x91 in f else None,
            name=f[0x92].decode("utf-8", "replace") if 0x92 in f else None,
        )


def _cc_required(smdp_signed2: bytes) -> bool:
    flag = tlv.find(tlv.first(smdp_signed2).value, 0x01)
    return bool(flag and flag.value and flag.value[0])


def _hash_cc(code: str, transaction_id: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(code.encode()).digest() + transaction_id).digest()


async def download_profile(card: Euicc, ac: ActivationCode, confirmation_code: str | None = None,
                           insecure_tls: bool = False, confirm=None,
                           ca_file: str | None = None) -> InstallResult:
    """Run the full SGP.22 download. `confirm(meta)` may veto before the BPP is fetched."""
    smdp = Es9pClient(ac.smdp, insecure=insecure_tls, ca_file=ca_file)

    log.info("reading eUICC info / challenge")
    info1 = await card.get_euicc_info1()
    challenge = await card.get_euicc_challenge()

    log.info("ES9+ initiateAuthentication -> %s", ac.smdp)
    r1 = smdp.initiate_authentication(challenge, info1)
    tx_id = r1["transactionId"]
    tx_bin = bytes.fromhex(tx_id)

    async def cancel(reason: int) -> None:
        try:
            resp = await card.cancel_session(tx_bin, reason)
            smdp.cancel_session(tx_id, resp)
            log.info("session %s cancelled (reason %d)", tx_id, reason)
        except Exception as e:  # noqa: BLE001
            log.warning("cancel session failed: %s", e)

    log.info("ES10b AuthenticateServer")
    auth = await card.authenticate_server(
        b64d(r1["serverSigned1"]), b64d(r1["serverSignature1"]),
        b64d(r1["euiccCiPKIdToBeUsed"]), b64d(r1["serverCertificate"]),
        ac.matching_id or None)

    log.info("ES9+ authenticateClient")
    try:
        r2 = smdp.authenticate_client(tx_id, auth)
    except SmdpError:
        await cancel(127)
        raise
    meta = ProfileMetadata.parse(b64d(r2["profileMetadata"]))
    log.info("profile offered: provider=%s name=%s iccid=%s", meta.provider, meta.name, meta.iccid)
    if confirm is not None and not confirm(meta):
        await cancel(0)  # endUserRejection
        raise CardError("download rejected by user")

    signed2 = b64d(r2["smdpSigned2"])
    hash_cc = None
    if _cc_required(signed2) or ac.cc_required:
        if not confirmation_code:
            await cancel(0)
            raise CardError("SM-DP+ requires a confirmation code (--cc)")
        hash_cc = _hash_cc(confirmation_code, tx_bin)

    log.info("ES10b PrepareDownload")
    try:
        prep = await card.prepare_download(signed2, b64d(r2["smdpSignature2"]),
                                           b64d(r2["smdpCertificate"]), hash_cc)
    except CardError:
        await cancel(127)
        raise

    log.info("ES9+ getBoundProfilePackage")
    r3 = smdp.get_bound_profile_package(tx_id, prep)
    bpp = b64d(r3["boundProfilePackage"])
    log.info("BPP %d bytes, loading into eUICC", len(bpp))

    def progress(i, n, sent, total):
        log.info("  segment %d/%d  %d/%d bytes (%d%%)", i, n, sent, total, sent * 100 // total)

    result = await card.load_bound_profile_package(bpp, progress)

    # Report the install result to the SM-DP+, then drop it from the card's list.
    try:
        target = Es9pClient(result.notification_address or ac.smdp,
                            insecure=insecure_tls, ca_file=ca_file)
        target.handle_notification(result.raw)
        if result.seq_number is not None:
            await card.notification_sent(result.seq_number)
        log.info("install notification delivered (seq %s)", result.seq_number)
    except Exception as e:  # noqa: BLE001 - install already done; card keeps it pending
        log.warning("notification not delivered (kept on card): %s", e)

    if not result.ok:
        raise CardError(f"install failed at {result.bpp_command}: {result.error_reason}")
    return result
