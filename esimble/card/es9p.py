"""ES9+ client (LPA <-> SM-DP+), JSON over HTTPS, GSMA SGP.22 section 6.5."""

from __future__ import annotations

import base64
import json
import logging
import os
import ssl
import urllib.error
import urllib.request

log = logging.getLogger("es9p")

HEADERS = {
    "User-Agent": "gsma-rsp-lpad",
    "X-Admin-Protocol": "gsma/rsp/v2.2.2",
    "Content-Type": "application/json",
    "Accept": "application/json",
}


class SmdpError(Exception):
    pass


def b64e(b: bytes) -> str:
    return base64.b64encode(b).decode()


def b64d(s: str) -> bytes:
    return base64.b64decode(s)


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:  # prefer certifi's bundle when the interpreter has no system roots
        import certifi
        ctx.load_verify_locations(certifi.where())
    except Exception:  # noqa: BLE001
        pass
    return ctx


class Es9pClient:
    def __init__(self, address: str, timeout: float = 60.0, insecure: bool = False,
                 ca_file: str | None = None):
        self.address = address.strip().rstrip("/")
        self.timeout = timeout
        self.ctx = _ssl_context()
        # SM-DP+ TLS certs chain to the GSMA RSP2 Root CI, not a public CA.
        ca_file = ca_file or os.environ.get("ESIMBLE_CA_FILE")
        if ca_file:
            self.ctx.load_verify_locations(ca_file)
        if insecure:  # only for a mock SM-DP+ with a self-signed certificate
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def _post(self, function: str, body: dict, expect_body: bool = True) -> dict:
        url = f"https://{self.address}/gsma/rsp2/es9plus/{function}"
        log.debug("POST %s", url)
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers=HEADERS, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise SmdpError(f"{function}: HTTP {e.code} {e.read()[:300]!r}") from e
        except urllib.error.URLError as e:
            raise SmdpError(f"{function}: {e.reason}") from e
        if not raw:
            if expect_body:
                raise SmdpError(f"{function}: empty response")
            return {}
        data = json.loads(raw)
        status = data.get("header", {}).get("functionExecutionStatus", {})
        if status.get("status") not in (None, "Executed-Success", "Executed-WithWarning"):
            sd = status.get("statusCodeData", {})
            raise SmdpError(
                f"{function}: {status.get('status')} subject={sd.get('subjectCode')} "
                f"reason={sd.get('reasonCode')} {sd.get('message', '')}".strip())
        return data

    def initiate_authentication(self, euicc_challenge: bytes, euicc_info1: bytes) -> dict:
        return self._post("initiateAuthentication", {
            "euiccChallenge": b64e(euicc_challenge),
            "euiccInfo1": b64e(euicc_info1),
            "smdpAddress": self.address,
        })

    def authenticate_client(self, transaction_id: str, auth_server_resp: bytes) -> dict:
        return self._post("authenticateClient", {
            "transactionId": transaction_id,
            "authenticateServerResponse": b64e(auth_server_resp),
        })

    def get_bound_profile_package(self, transaction_id: str, prepare_resp: bytes) -> dict:
        return self._post("getBoundProfilePackage", {
            "transactionId": transaction_id,
            "prepareDownloadResponse": b64e(prepare_resp),
        })

    def cancel_session(self, transaction_id: str, cancel_resp: bytes) -> None:
        self._post("cancelSession", {
            "transactionId": transaction_id,
            "cancelSessionResponse": b64e(cancel_resp),
        }, expect_body=False)

    def handle_notification(self, pending_notification: bytes) -> None:
        self._post("handleNotification",
                   {"pendingNotification": b64e(pending_notification)},
                   expect_body=False)
