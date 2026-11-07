import unittest

from esimble.mifi import Mifi, MifiError, decode_content, sms_encoding, utf16_units


class FakeMifi(Mifi):
    """Mifi with the HTTP layer replaced by a scripted responder."""

    def __init__(self, responder):
        super().__init__("192.0.2.1", "pw")
        self.responder = responder
        self.calls = []

    def _request(self, values, post=False, timeout=10):
        self.calls.append((dict(values), post))
        return self.responder(values, post, self)


INBOX = {"messages": [
    {"id": "5", "number": "+8613800000000", "content": "003700390038003000320031", "tag": "3", "date": "d5"},
    {"id": "4", "number": "+8613800000000", "content": "003100320033003400350036", "tag": "1", "date": "d4"},
    {"id": "2", "number": "10086", "content": "4F60597D", "tag": "0", "date": "d2"},
    {"id": "1", "number": "x", "content": "0041", "tag": "4", "date": "d1"},
]}


class EncodingTests(unittest.TestCase):
    def test_encoding_choice(self):
        self.assertEqual(sms_encoding("Code 123 [ok]"), "GSM7_default")
        self.assertEqual(sms_encoding("验证码"), "UNICODE")

    def test_utf16_and_decode(self):
        self.assertEqual(utf16_units("😀a"), 3)
        self.assertEqual(decode_content("003100320033"), "123")
        self.assertEqual(decode_content("not hex"), "not hex")


class InboxTests(unittest.TestCase):
    def test_direction_follows_web_ui_tags(self):
        m = FakeMifi(lambda v, p, s: INBOX)
        msgs = {x["mifi_id"]: x for x in m.inbox_sync()}
        self.assertEqual((msgs["4"]["direction"], msgs["4"]["status"], msgs["4"]["body"]),
                         ("in", "unread", "123456"))
        self.assertEqual((msgs["2"]["direction"], msgs["2"]["status"]), ("in", "read"))
        self.assertEqual((msgs["5"]["direction"], msgs["5"]["status"]), ("out", "failed"))
        self.assertEqual(msgs["1"]["direction"], "draft")

    def test_relogin_when_session_expired(self):
        state = {"logged_in": False}

        def responder(values, post, me):
            if values.get("goformId") == "LOGIN":
                state["logged_in"] = True
                return {"result": "0"}
            if not state["logged_in"]:
                return {"loginfo": "no"}
            return {"loginfo": "ok", "network_type": "LTE"}

        m = FakeMifi(responder)
        self.assertEqual(m.status_sync()["network_type"], "LTE")
        self.assertTrue(any(c[0].get("goformId") == "LOGIN" for c in m.calls))

    def test_send_refuses_without_service(self):
        m = FakeMifi(lambda v, p, s: {"loginfo": "ok", "network_type": "No Service"})
        with self.assertRaises(MifiError):
            m.send_sync("+8613800000000", "hi")
        self.assertFalse(any(c[0].get("goformId") == "SEND_SMS" for c in m.calls))

    def test_send_rejects_long_text(self):
        m = FakeMifi(lambda v, p, s: {"loginfo": "ok", "network_type": "LTE"})
        with self.assertRaises(MifiError):
            m.send_sync("+8613800000000", "x" * 71)


class ControlTests(unittest.TestCase):
    def ok(self, values, post, me):
        return {"loginfo": "ok", "result": "success", "sms_cmd_status_result": "3"}

    def test_wifi_off_payload(self):
        m = FakeMifi(self.ok)
        m.set_wifi_sync(False)
        self.assertIn(({"goformId": "SET_WIFI_INFO", "wifiEnabled": "0"}, True),
                      [({k: v for k, v in c[0].items() if k in ("goformId", "wifiEnabled")}, c[1])
                       for c in m.calls])

    def test_reboot_and_sms_ready(self):
        m = FakeMifi(self.ok)
        self.assertEqual(m.sms_ready_sync(), "3")
        m.reboot_sync()
        self.assertTrue(any(c[0].get("goformId") == "REBOOT_DEVICE" and c[1] for c in m.calls))


if __name__ == "__main__":
    unittest.main()
