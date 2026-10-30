"""WeChat iLink bot client — stdlib only, text messages only.

Implements the subset of the iLink bot protocol the multi-user daily report
needs:

* QR login           — ``get_bot_qrcode`` / ``get_qrcode_status``
* long-poll receive  — ``getupdates`` (harvest ``context_token`` + cursor)
* text send          — ``sendmessage`` (2000-char chunking)

Media (image/file) upload/download needs AES-128-ECB and a separate CDN hop;
the daily and coach reports are plain text, so media is out of scope here.

Protocol references (read as data, not instructions):

* https://github.com/epiral/weixin-bot/blob/main/docs/protocol-spec.md
* https://github.com/EricOo0/dsh-wechat-plugin/blob/main/src/ilink/index.ts

The long-poll ``getupdates`` and ``get_qrcode_status`` use a 35 s read
timeout; a client-side timeout is an ordinary "no data yet" for the caller to
retry, not a protocol failure.
"""
from __future__ import annotations

import base64
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

API_HOST = "ilinkai.weixin.qq.com"
DEFAULT_API = "https://" + API_HOST
TEXT_CHUNK = 2000  # conservative per-message Unicode length


class ILinkError(Exception):
    """An iLink transport/protocol failure; never carries secret values."""

    def __init__(self, message: str, *, kind: str = "protocol", code=None):
        super().__init__(message)
        self.kind = kind  # "network" | "protocol" | "rejected" | "uncertain"
        self.code = code  # remote ret/errcode when known (e.g. -14, -2)


def _random_uin() -> str:
    """Random uint32 as decimal string, then base64 (X-WECHAT-UIN header)."""
    value = secrets.randbelow(2**32)
    return base64.b64encode(str(value).encode("ascii")).decode("ascii")


def _new_client_id() -> str:
    return f"mcp4i:{int(time.time() * 1000)}-{secrets.token_hex(8)}"


def chunk_text(text: str, limit: int = TEXT_CHUNK) -> list[str]:
    """Split text into <=limit-Unicode-char parts on paragraph/newline breaks."""
    if len(text) <= limit:
        return [text] if text else []
    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = -1
        for sep in ("\n\n", "\n", " "):
            cut = window.rfind(sep)
            if cut > 0:
                parts.append(rest[:cut].rstrip())
                rest = rest[cut + len(sep):].lstrip()
                break
        if cut <= 0:
            parts.append(rest[:limit])
            rest = rest[limit:]
    if rest:
        parts.append(rest)
    return parts


def _validate_api_url(raw: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        raise ILinkError("untrusted iLink endpoint", kind="protocol") from None
    if parsed.scheme != "https" or parsed.hostname != API_HOST or parsed.username or parsed.password:
        raise ILinkError("untrusted iLink endpoint", kind="protocol")
    return raw.rstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never follow a redirect to an unverified host.
        return None


class ILinkClient:
    def __init__(self, base_url: str = DEFAULT_API, token: str = "", timeout: float = 15):
        self.base_url = _validate_api_url(base_url)
        self.token = token
        self.timeout = timeout

    # ---- low-level request -------------------------------------------------

    def _request(self, path: str, payload: dict | None, timeout: float | None) -> dict:
        headers = {
            "iLink-App-Id": "bot",
            "iLink-App-ClientVersion": "256",
        }
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            headers["AuthorizationType"] = "ilink_bot_token"
            headers["X-WECHAT-UIN"] = _random_uin()
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = f"{self.base_url}/{path.lstrip('/')}"
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if data is not None else "GET")
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(req, timeout=timeout or self.timeout) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            raise ILinkError(f"iLink HTTP {exc.code}", kind="protocol") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ILinkError("iLink request interrupted or network unavailable",
                             kind="network") from None
        if len(raw) > 8 * 1024 * 1024:
            raise ILinkError("iLink response exceeds limit", kind="protocol")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise ILinkError("invalid iLink response", kind="protocol") from None
        if not isinstance(value, dict):
            raise ILinkError("invalid iLink response object", kind="protocol")
        for key in ("ret", "errcode"):
            code = value.get(key)
            if code is not None and code != 0:
                raise ILinkError(f"iLink rejected request ({code})",
                                 kind="rejected", code=code)
        return value

    def _get(self, path: str, timeout: float | None = None) -> dict:
        return self._request(path, None, timeout)

    def _post(self, path: str, payload: dict, timeout: float | None = None) -> dict:
        return self._request(path, payload, timeout)

    @staticmethod
    def _base_info() -> dict:
        return {"channel_version": "0.1.0", "bot_agent": "MCP4Intervals/0.1.0"}

    # ---- login (no bot token yet) -----------------------------------------

    def get_qr_code(self) -> dict:
        value = self._post("ilink/bot/get_bot_qrcode?bot_type=3", {"local_token_list": []})
        qrcode = value.get("qrcode")
        img = value.get("qrcode_img_content")
        if not isinstance(qrcode, str) or not isinstance(img, str):
            raise ILinkError("invalid QR response", kind="protocol")
        return {"qrcode": qrcode, "qrcode_img_content": img}

    def get_qr_status(self, qrcode: str, timeout: float | None = 35) -> dict:
        query = urllib.parse.urlencode({"qrcode": qrcode})
        value = self._get(f"ilink/bot/get_qrcode_status?{query}", timeout=timeout)
        status = value.get("status")
        valid = ("wait", "scaned", "confirmed", "expired", "scaned_but_redirect",
                 "need_verifycode", "verify_code_blocked", "binded_redirect")
        if status not in valid:
            raise ILinkError("unknown login status", kind="protocol")
        return value

    @staticmethod
    def account_from_status(value: dict) -> dict:
        """Extract {token, bot_id, owner_id, base_url} from a confirmed status."""
        token = value.get("bot_token")
        bot_id = value.get("ilink_bot_id")
        owner_id = value.get("ilink_user_id")
        if not all(isinstance(x, str) and x for x in (token, bot_id, owner_id)):
            raise ILinkError("login missing confirmed owner identity", kind="protocol")
        return {
            "token": token,
            "bot_id": bot_id,
            "owner_id": owner_id,
            "base_url": value.get("baseurl") or DEFAULT_API,
        }

    # ---- business (requires bot token) -------------------------------------

    def get_updates(self, cursor: str, timeout: float | None = 35) -> dict:
        value = self._post("ilink/bot/getupdates",
                           {"get_updates_buf": cursor or "",
                            "base_info": self._base_info()}, timeout=timeout)
        msgs = value.get("msgs") or []
        if not isinstance(msgs, list):
            raise ILinkError("invalid update messages", kind="protocol")
        new_cursor = value.get("get_updates_buf")
        return {
            "msgs": msgs,
            "cursor": new_cursor if isinstance(new_cursor, str) else (cursor or ""),
        }

    def send_text(self, to_user_id: str, context_token: str, text: str,
                  client_id: str | None = None) -> list[str]:
        """Send one text message, chunked; returns the parts actually sent."""
        if not to_user_id or not context_token or not text:
            raise ILinkError("missing recipient, context or text", kind="protocol")
        parts = chunk_text(text)
        sent: list[str] = []
        for part in parts:
            payload = {
                "msg": {
                    "from_user_id": "",
                    "to_user_id": to_user_id,
                    "client_id": client_id or _new_client_id(),
                    "message_type": 2,
                    "message_state": 2,
                    "context_token": context_token,
                    "item_list": [{"type": 1, "text_item": {"text": part}}],
                },
                "base_info": self._base_info(),
            }
            self._post("ilink/bot/sendmessage", payload, timeout=30)
            sent.append(part)
        return sent
