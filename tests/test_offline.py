import asyncio
import unittest

from esimble.card import tlv
from esimble.card.download import ActivationCode, _hash_cc
from esimble.card.euicc import Euicc, InstallResult
from esimble.card.transport import ReplayTransport

OK = bytes.fromhex("9000")


def run(coro):
    return asyncio.run(coro)


def opened(script):
    """Script prefix for open_isdr: 3 closes, terminal cap, manage channel -> ch1, select."""
    return [(b"\x00\x70\x80\x01", OK), (b"\x00\x70\x80\x02", OK), (b"\x00\x70\x80\x03", OK),
            (bytes.fromhex("80AA"), OK), (bytes.fromhex("0070000001"), bytes.fromhex("019000")),
            (bytes.fromhex("01A40400"), OK)] + script


class Tests(unittest.TestCase):
    def test_activation_code(self):
        ac = ActivationCode.parse("LPA:1$consumer.rsp.world$LQQB6GVU2CG4KECJ6D3WUBCC")
        self.assertEqual(ac.smdp, "consumer.rsp.world")
        self.assertEqual(ac.matching_id, "LQQB6GVU2CG4KECJ6D3WUBCC")
        self.assertFalse(ac.cc_required)

    def test_tlv_roundtrip(self):
        v = bytes(300)
        t = tlv.first(tlv.encode(0xBF36, v))
        self.assertEqual((t.tag, t.value, t.header_len), (0xBF36, v, 5))
        self.assertEqual(tlv.decode_bcd_swapped(tlv.encode_bcd_swapped("8986012345678901234")),
                         "8986012345678901234")

    def test_list_profiles_on_logical_channel_with_get_response(self):
        e3 = tlv.encode(0xE3, tlv.encode(0x5A, tlv.encode_bcd_swapped("89860000000000000001"))
                        + tlv.encode(0x9F70, b"\x01") + tlv.encode(0x92, b"Test"))
        resp = tlv.encode(0xBF2D, tlv.encode(0xA0, e3))
        t = ReplayTransport(opened([
            (bytes.fromhex("81E2910003BF2D00"), resp[:10] + bytes([0x61, len(resp) - 10])),
            (bytes.fromhex("01C00000"), resp[10:] + OK),
        ]))
        card = Euicc(t)
        run(card.open_isdr())
        ps = run(card.list_profiles())
        self.assertEqual(card.channel, 1)
        self.assertEqual(ps[0].iccid, "89860000000000000001")
        self.assertEqual(ps[0].state, "enabled")

    def test_store_data_blocks(self):
        req = bytes(250)
        t = ReplayTransport([(bytes.fromhex("80E21100"), OK), (bytes.fromhex("80E21101"), OK),
                             (bytes.fromhex("80E29102"), bytes.fromhex("BF2E00") + OK)])
        card = Euicc(t, mss=100)
        self.assertEqual(run(card.es10(req)), bytes.fromhex("BF2E00"))
        self.assertEqual([len(a) for a in t.sent], [105, 105, 55])

    def test_bpp_segmentation_and_result(self):
        bpp = tlv.encode(0xBF36,
                         tlv.encode(0xBF23, b"i" * 10) + tlv.encode(0xA0, tlv.encode(0x87, b"c"))
                         + tlv.encode(0xA1, tlv.encode(0x88, b"m1") + tlv.encode(0x88, b"m2"))
                         + tlv.encode(0xA3, tlv.encode(0x86, b"p" * 200) + tlv.encode(0x86, b"q")))
        meta = tlv.encode(0xBF2F, tlv.encode(0x80, b"\x05")
                          + tlv.encode(0x5A, tlv.encode_bcd_swapped("89000000000000000042")))
        result = tlv.encode(0xBF37, tlv.encode(0xBF27, meta + tlv.encode(0xA2, tlv.encode(0xA0, b""))))
        # segments: BF36hdr+BF23, A0, A1hdr, 88, 88, A3hdr, 86(203 bytes -> 2 blocks), 86
        script = [(b"\x80\xE2\x91", OK)] * 6 + [(b"\x80\xE2\x11\x00", OK),
                                               (b"\x80\xE2\x91\x01", OK),
                                               (b"\x80\xE2\x91\x00", result + OK)]
        card = Euicc(ReplayTransport(script))
        r = run(card.load_bound_profile_package(bpp))
        self.assertTrue(r.ok)
        self.assertEqual((r.seq_number, r.iccid), (5, "89000000000000000042"))

    def test_install_error_parse(self):
        meta = tlv.encode(0xBF2F, tlv.encode(0x80, b"\x01"))
        err = tlv.encode(0xA1, tlv.encode(0x80, b"\x05") + tlv.encode(0x81, b"\x09"))
        r = InstallResult.parse(tlv.encode(0xBF37, tlv.encode(0xBF27, meta + tlv.encode(0xA2, err))))
        self.assertFalse(r.ok)
        self.assertEqual(r.error_reason, "installFailedDueToIccidAlreadyExistsOnEuicc")

    def test_hash_cc(self):
        self.assertEqual(len(_hash_cc("1234", b"\x01\x02")), 32)


if __name__ == "__main__":
    unittest.main()
