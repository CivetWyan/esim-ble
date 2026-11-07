"""REST API + admin web UI (FastAPI)."""

from __future__ import annotations

import hmac
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from ..card.download import ActivationCode
from ..card.euicc import CardError
from ..mifi import MifiError
from .engine import Engine, PlatformError
from .store import extract_code, iccid_match

log = logging.getLogger("api")
STATIC = Path(__file__).parent / "static"
COOKIE = "esim_token"

OFFLINE_NOTE = ("号码尚未在线：离线期间发给它的短信可能延迟或丢失。"
                "等 online=true 之后再让对方发送短信。")

LOGIN_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>eSIM 接码平台 登录</title>
<style>body{font-family:-apple-system,system-ui,sans-serif;background:#f5f6f8;display:flex;
align-items:center;justify-content:center;height:100vh;margin:0}form{background:#fff;padding:28px;
border-radius:10px;box-shadow:0 2px 12px #0001;width:340px}input{width:100%;padding:9px;
box-sizing:border-box;margin:12px 0;border:1px solid #ccc;border-radius:6px}button{width:100%;
padding:9px;border:0;border-radius:6px;background:#2563eb;color:#fff;font-size:15px}
p{color:#666;font-size:13px}</style></head><body><form method="get" action="/login">
<b>eSIM 接码平台</b><p>输入 API 令牌。启动日志里有带令牌的登录链接，也可以运行
<code>python -m esimble.platform token</code> 查看。</p>
<input name="token" type="password" placeholder="API token" autofocus>
<button>登录</button></form></body></html>"""


class NumberPatch(BaseModel):
    label: str | None = None
    msisdn: str | None = None
    notes: str | None = None
    tags: str | None = None
    poll_enabled: bool | None = None
    priority: int | None = None
    card_nickname: str | None = None   # written to the card over Bluetooth


class SwitchBody(BaseModel):
    hold_seconds: int | None = None


class WaitBody(BaseModel):
    number: str
    since_id: int | None = None
    timeout: float = 45
    sender: str | None = None
    contains: str | None = None


class SendBody(BaseModel):
    number: str
    to: str
    text: str


def create_app(engine: Engine, manage_engine: bool = True) -> FastAPI:
    s = engine.s

    @asynccontextmanager
    async def lifespan(app):
        if manage_engine:
            engine.start()
        yield
        if manage_engine:
            await engine.stop()

    app = FastAPI(title="esim-ble SMS platform", version="1.0", lifespan=lifespan,
                  docs_url="/api/docs", openapi_url="/api/openapi.json")

    allowed_hosts = {"127.0.0.1", "localhost", "::1", "[::1]", s.host}

    @app.middleware("http")
    async def host_guard(request: Request, call_next):
        host = (request.headers.get("host") or "").rsplit(":", 1)[0]
        if host and host not in allowed_hosts:
            return JSONResponse({"detail": "host not allowed"}, status_code=403)
        return await call_next(request)

    def _token_ok(tok: str | None) -> bool:
        return bool(tok) and hmac.compare_digest(tok, s.api_token)

    def caller(request: Request) -> str:
        """Returns the channel: web (cookie) or rest/sdk/mcp (bearer)."""
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer ") and _token_ok(auth[7:].strip()):
            c = request.headers.get("x-client", "rest").lower()
            return c if c in ("rest", "sdk", "mcp") else "rest"
        if _token_ok(request.cookies.get(COOKIE)):
            # cookie auth is for the same-origin admin page only (CSRF guard)
            if request.method != "GET" and request.headers.get("x-requested-with") != "esim-admin":
                raise HTTPException(403, "missing admin header")
            return "web"
        raise HTTPException(401, "missing or invalid token")

    def device_free():
        if engine.device_lock.locked():
            raise HTTPException(409, f"device busy ({engine.phase}: {engine.phase_detail}); "
                                     "try again in a minute")

    def err(e: Exception) -> HTTPException:
        if isinstance(e, HTTPException):
            return e
        if isinstance(e, PlatformError):
            return HTTPException(400, str(e))
        if isinstance(e, (CardError, MifiError, ValueError)):
            return HTTPException(502 if isinstance(e, MifiError) else 400,
                                 f"{type(e).__name__}: {e}")
        log.exception("unexpected")
        return HTTPException(500, f"{type(e).__name__}: {e}")

    def number_view(p: dict, counts: dict | None = None) -> dict:
        counts = counts if counts is not None else engine.db.counts()
        c = counts.get(p["iccid"], {})
        online = iccid_match(p["iccid"], engine.current_iccid) and engine.phase == "online"
        return {**{k: p[k] for k in (
            "iccid", "label", "msisdn", "notes", "tags", "poll_enabled", "priority",
            "card_provider", "card_name", "card_nickname", "card_state", "profile_class",
            "on_card", "imsi", "last_active_at", "last_switch_error", "created_at",
            "updated_at")}, "online": online,
            "modem_active": iccid_match(p["iccid"], engine.current_iccid),
            "is_home": bool(s.home_iccid) and iccid_match(p["iccid"], s.home_iccid),
            "message_count": c.get("n", 0), "last_message_id": c.get("last_id")}

    # ------------------------------------------------------------- web pages
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        if not _token_ok(request.cookies.get(COOKIE)):
            return HTMLResponse(LOGIN_HTML)
        return FileResponse(STATIC / "index.html")

    @app.get("/login")
    async def login(token: str = ""):
        if not _token_ok(token):
            return HTMLResponse(LOGIN_HTML.replace("<b>eSIM", "<b style=color:#c00>令牌错误</b><br><b>eSIM"),
                                status_code=401)
        r = RedirectResponse("/", status_code=303)
        r.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=30 * 86400)
        return r

    @app.get("/logout")
    async def logout():
        r = RedirectResponse("/", status_code=303)
        r.delete_cookie(COOKIE)
        return r

    # ---------------------------------------------------------------- status
    @app.get("/api/v1/status")
    async def status(via: str = Depends(caller)):
        st = engine.status()
        st["settings"] = s.public()
        st["card"] = {k: (engine.card_info or {}).get(k) for k in ("eid", "free_nvm")}
        return st

    @app.get("/api/v1/events")
    async def events(limit: int = 100, via: str = Depends(caller)):
        return engine.db.events(min(limit, 500))

    @app.get("/api/v1/settings")
    async def get_settings(via: str = Depends(caller)):
        return s.public()

    @app.patch("/api/v1/settings")
    async def patch_settings(body: dict[str, Any], via: str = Depends(caller)):
        if via != "web":
            raise HTTPException(403, "settings can only be changed from the admin page")
        s.update(body)
        s.save()
        engine.db.event("settings", f"settings changed: {sorted(body)}")
        engine.wake()
        return s.public()

    # --------------------------------------------------------------- numbers
    @app.get("/api/v1/numbers")
    async def numbers(include_removed: bool = False, via: str = Depends(caller)):
        counts = engine.db.counts()
        return [number_view(p, counts) for p in engine.db.profiles(include_removed)]

    @app.get("/api/v1/numbers/{key}")
    async def number(key: str, via: str = Depends(caller)):
        try:
            return number_view(engine.require_profile(key))
        except PlatformError as e:
            raise HTTPException(404, str(e))


    @app.patch("/api/v1/numbers/{key}")
    async def patch_number(key: str, body: NumberPatch, via: str = Depends(caller)):
        try:
            p = engine.require_profile(key)
            fields = body.model_dump(exclude_none=True)
            nick = fields.pop("card_nickname", None)
            if "poll_enabled" in fields:
                fields["poll_enabled"] = int(fields["poll_enabled"])
            if "msisdn" in fields:
                fields["msisdn"] = fields["msisdn"].replace(" ", "")
            if fields:
                engine.db.update_profile(p["iccid"], **fields)
            if nick is not None and nick != p["card_nickname"]:
                device_free()
                await engine.rename_profile(p["iccid"], nick)
            engine.db.event("profile", f"{p['iccid']} updated via {via}: {sorted(body.model_dump(exclude_none=True))}")
            engine.wake()
            return number_view(engine.require_profile(p["iccid"]))
        except Exception as e:  # noqa: BLE001
            raise err(e)

    @app.post("/api/v1/numbers")
    async def add_number(activation_code: str = Form(""), nickname: str = Form(""),
                         confirmation_code: str = Form(""), label: str = Form(""),
                         msisdn: str = Form(""), notes: str = Form(""), tags: str = Form(""),
                         qr: UploadFile | None = File(None), via: str = Depends(caller)):
        try:
            code = activation_code.strip()
            if not code and qr is not None:
                code = decode_qr(await qr.read())
            if not code:
                raise HTTPException(400, "activation_code or qr image required")
            ActivationCode.parse(code)
            device_free()
            engine.db.event("card", f"install requested via {via}")
            r = await engine.add_profile(code, nickname, confirmation_code)
            upd = {k: v for k, v in (("label", label or nickname), ("msisdn", msisdn),
                                     ("notes", notes), ("tags", tags)) if v}
            if upd:
                engine.db.update_profile(r["iccid"], **upd)
            return number_view(engine.require_profile(r["iccid"]))
        except Exception as e:  # noqa: BLE001
            raise err(e)

    @app.post("/api/v1/qr/decode")
    async def qr_decode(qr: UploadFile = File(...), via: str = Depends(caller)):
        try:
            code = decode_qr(await qr.read())
            ac = ActivationCode.parse(code)
            return {"activation_code": code, "smdp": ac.smdp}
        except Exception as e:  # noqa: BLE001
            raise err(e)

    @app.delete("/api/v1/numbers/{key}")
    async def delete_number(key: str, confirm: str = "", via: str = Depends(caller)):
        try:
            p = engine.require_profile(key)
            if confirm != p["iccid"]:
                raise HTTPException(400, "pass ?confirm=<full ICCID> to delete from the card")
            device_free()
            await engine.delete_profile(p["iccid"])
            engine.db.event("card", f"deleted {p['iccid']} via {via}", "warning")
            return {"deleted": p["iccid"]}
        except Exception as e:  # noqa: BLE001
            raise err(e)

    @app.post("/api/v1/card/refresh")
    async def card_refresh(via: str = Depends(caller)):
        try:
            device_free()
            return await engine.refresh_card()
        except Exception as e:  # noqa: BLE001
            raise err(e)


    @app.post("/api/v1/numbers/{key}/receive")
    @app.post("/api/v1/numbers/{key}/switch")
    async def switch(key: str, body: SwitchBody | None = None, via: str = Depends(caller)):
        """Bring a number online now and keep it there for hold_seconds."""
        if via == "mcp":
            raise HTTPException(403, "use prepare/wait from MCP")
        try:
            h = engine.hold(key, (body.hold_seconds if body else None), reason="manual")
            engine.db.event("switch", f"receive session for {h.iccid} via {via}")
            return {"hold_id": h.id, "iccid": h.iccid, "until": h.until,
                    "cursor": engine.db.max_message_id(),
                    "eta_seconds": engine.eta_seconds(h.iccid)}
        except Exception as e:  # noqa: BLE001
            raise err(e)

    @app.delete("/api/v1/holds/{hold_id}")
    async def release(hold_id: int, via: str = Depends(caller)):
        engine.release(hold_id)
        return {"released": hold_id}

    @app.post("/api/v1/holds/{hold_id}/extend")
    async def extend(hold_id: int, seconds: int = 600, via: str = Depends(caller)):
        try:
            h = engine.extend(hold_id, max(60, min(seconds, 3600)))
            return {"hold_id": h.id, "until": h.until}
        except Exception as e:  # noqa: BLE001
            raise err(e)

    @app.post("/api/v1/numbers/{key}/prepare")
    async def prepare(key: str, via: str = Depends(caller)):
        """Start bringing the number online; returns a cursor to wait from."""
        try:
            p = engine.require_profile(key)
            h = engine.hold(p["iccid"])
            return {"number": engine._pub(p), "cursor": engine.db.max_message_id(),
                    "online": iccid_match(p["iccid"], engine.current_iccid)
                    and engine.phase == "online",
                    "eta_seconds": engine.eta_seconds(p["iccid"]), "hold_until": h.until,
                    "phase": engine.phase, "note": OFFLINE_NOTE
                    if not (iccid_match(p["iccid"], engine.current_iccid)
                            and engine.phase == "online") else ""}
        except Exception as e:  # noqa: BLE001
            raise err(e)

    # -------------------------------------------------------------- messages
    @app.get("/api/v1/messages")
    async def messages(number: str = "", direction: str = "", since_id: int = 0,
                       before_id: int = 0, limit: int = 50, sender: str = "",
                       contains: str = "", via: str = Depends(caller)):
        try:
            iccid = engine.require_profile(number)["iccid"] if number else None
            return engine.db.find_messages(iccid, direction or None, since_id, limit,
                                           sender or None, contains or None, before_id or None)
        except PlatformError as e:
            raise HTTPException(404, str(e))

    @app.get("/api/v1/messages/latest-code")
    async def latest_code(number: str, since_id: int = 0, max_age_seconds: int = 1800,
                          sender: str = "", via: str = Depends(caller)):
        try:
            p = engine.require_profile(number)
        except PlatformError as e:
            raise HTTPException(404, str(e))
        for m in engine.db.find_messages(p["iccid"], "in", since_id=0, limit=50,
                                         sender=sender or None):
            if m["id"] <= since_id:
                break
            if m["code"] and _age(m) <= max_age_seconds:
                return {"code": m["code"], "message": m}
        return {"code": None, "message": None}


    @app.post("/api/v1/wait")
    async def wait(body: WaitBody, via: str = Depends(caller)):
        try:
            since = body.since_id if body.since_id is not None else engine.db.max_message_id()
            r = await engine.wait_for_message(body.number, since, body.timeout,
                                              body.sender, body.contains)
            for m in r["messages"]:
                m.setdefault("code", extract_code(m["body"]))
            r["code"] = next((m["code"] for m in r["messages"] if m["code"]), None)
            if r["status"] == "pending" and not r.get("online"):
                r["note"] = OFFLINE_NOTE
            return r
        except Exception as e:  # noqa: BLE001
            raise err(e)


    @app.post("/api/v1/send")
    async def send(body: SendBody, via: str = Depends(caller)):
        if via != "web" and not s.api_allow_send:
            raise HTTPException(403, "sending via API is disabled (settings: api_allow_send)")
        try:
            return await engine.send(body.number, body.to, body.text, via)
        except Exception as e:  # noqa: BLE001
            raise err(e)

    @app.delete("/api/v1/messages/{mid}")
    async def delete_message(mid: int, via: str = Depends(caller)):
        if via != "web":
            raise HTTPException(403, "only from the admin page")
        engine.db.delete_message(mid)
        return {"deleted": mid}

    @app.post("/api/v1/poll")
    async def poll_now(via: str = Depends(caller)):
        engine.wake()
        return {"ok": True}

    return app


def _age(m: dict) -> float:
    from datetime import datetime
    try:
        return time.time() - datetime.fromisoformat(m["received_at"]).timestamp()
    except (TypeError, ValueError):
        return 0


def decode_qr(data: bytes) -> str:
    try:
        import cv2
        import numpy as np
    except ImportError as e:
        raise ValueError("QR decoding needs opencv-python-headless") from e
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("cannot read image")
    text = cv2.QRCodeDetector().detectAndDecode(img)[0]
    if not text:
        raise ValueError("no QR code found in image")
    return text
