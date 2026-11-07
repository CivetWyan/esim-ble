import unittest

from esimble.card.nble import decode_frames, encode_frames


class NbleFrameTests(unittest.TestCase):
    def test_upstream_web_and_native_vectors(self):
        apdu = bytes.fromhex("01a4040010a0000005591010ffffffff890000010000")
        self.assertEqual(
            [frame.hex() for frame in encode_frames(4, apdu, 20)],
            [
                "000401a4040010a0000005591010ffffffff8900",
                "21000100000661",
            ],
        )
        self.assertEqual(
            [frame.hex() for frame in encode_frames(4, apdu, 101)],
            ["200401a4040010a0000005591010ffffffff8900000100000661"],
        )

    def test_response_round_trip_and_rejections(self):
        response = bytes((1, 2, 0x90, 0))
        frames = encode_frames(255, response, 3)
        self.assertEqual(decode_frames(frames, expected_counter=255), response)

        corrupt = list(frames)
        corrupt[0] = bytes((corrupt[0][0], corrupt[0][1] ^ 1)) + corrupt[0][2:]
        with self.assertRaises(ValueError):
            decode_frames(corrupt, expected_counter=255)
        with self.assertRaises(ValueError):
            decode_frames(frames, expected_counter=0)
        reordered = list(frames)
        reordered[0], reordered[1] = reordered[1], reordered[0]
        with self.assertRaises(ValueError):
            decode_frames(reordered, expected_counter=255)

    def test_250_byte_apdu_uses_three_frames(self):
        frames = encode_frames(0, bytes(250))
        self.assertEqual(len(frames), 3)
        self.assertEqual([frame[0] for frame in frames], [0x00, 0x01, 0x22])


if __name__ == "__main__":
    unittest.main()
