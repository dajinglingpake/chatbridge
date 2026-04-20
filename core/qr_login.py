from __future__ import annotations

import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Iterator

from core.http_json import request_json
from core.state_models import JsonObject


DEFAULT_HEADERS = {
    "AuthorizationType": "ilink_bot_token",
    "iLink-App-Id": "bot",
    "iLink-App-ClientVersion": "131073",
}


@dataclass
class QRCodePayload:
    code: str
    image_content: str

    @classmethod
    def from_dict(cls, raw: object) -> "QRCodePayload":
        if not isinstance(raw, dict):
            return cls(code="", image_content="")
        return cls(
            code=str(raw.get("qrcode") or "").strip(),
            image_content=str(raw.get("qrcode_img_content") or "").strip(),
        )


@dataclass
class QRStatusPayload:
    status: str
    redirect_host: str = ""
    raw_payload: JsonObject = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: object) -> "QRStatusPayload":
        if not isinstance(raw, dict):
            return cls(status="")
        return cls(
            status=str(raw.get("status") or "").strip(),
            redirect_host=str(raw.get("redirect_host") or "").strip(),
            raw_payload=dict(raw),
        )


@dataclass
class QRLoginEvent:
    type: str
    payload: JsonObject

    @property
    def base_url(self) -> str:
        return str(self.payload.get("base_url") or "").strip()

    @property
    def image_content(self) -> str:
        return str(self.payload.get("qrcode_img_content") or "").strip()

    @property
    def message(self) -> str:
        return str(self.payload.get("message") or "").strip()


def _read_json(url: str, headers: dict[str, str], timeout: int) -> JsonObject:
    request = urllib.request.Request(url, headers=headers)
    return request_json(request, timeout=timeout)


def fetch_bot_qr_code(base_url: str, headers: dict[str, str] | None = None, timeout: int = 15) -> QRCodePayload:
    payload = _read_json(f"{base_url}/ilink/bot/get_bot_qrcode?bot_type=3", headers or DEFAULT_HEADERS, timeout)
    return QRCodePayload.from_dict(payload)


def fetch_qr_status(base_url: str, qr_code: str, headers: dict[str, str] | None = None, timeout: int = 60) -> QRStatusPayload:
    encoded = urllib.parse.quote(qr_code)
    return QRStatusPayload.from_dict(
        _read_json(f"{base_url}/ilink/bot/get_qrcode_status?qrcode={encoded}", headers or DEFAULT_HEADERS, timeout)
    )


def iter_qr_login_events(
    base_url: str,
    logger: Callable[[str], None] | None = None,
    headers: dict[str, str] | None = None,
    poll_interval_seconds: float = 1.0,
    max_refresh: int = 3,
) -> Iterator[QRLoginEvent]:
    active_headers = headers or DEFAULT_HEADERS
    active_base_url = base_url
    qr = fetch_bot_qr_code(active_base_url, active_headers)
    if not qr.code or not qr.image_content:
        yield QRLoginEvent(type="error", payload={"message": "missing qrcode payload"})
        return

    if logger:
        logger(f"qrcode={qr.code}, qr_url={qr.image_content}")
    yield QRLoginEvent(type="qr_code", payload={"qrcode": qr.code, "qrcode_img_content": qr.image_content, "base_url": active_base_url})

    scanned = False
    refresh_count = 0
    current_qr_code = qr.code

    while True:
        status_data = fetch_qr_status(active_base_url, current_qr_code, active_headers)
        if logger:
            logger(f"poll response: status={status_data.status}")

        if status_data.status == "confirmed":
            yield QRLoginEvent(type="confirmed", payload=status_data.raw_payload)
            return

        if status_data.status == "scaned":
            if not scanned:
                scanned = True
                yield QRLoginEvent(type="scanned", payload=status_data.raw_payload)
        elif status_data.status == "expired":
            refresh_count += 1
            if refresh_count > max_refresh:
                yield QRLoginEvent(type="expired", payload={"refresh_count": refresh_count})
                return
            refreshed = fetch_bot_qr_code(active_base_url, active_headers, timeout=10)
            if not refreshed.code or not refreshed.image_content:
                yield QRLoginEvent(type="error", payload={"message": "failed to refresh qrcode"})
                return
            current_qr_code = refreshed.code
            scanned = False
            yield QRLoginEvent(
                type="qr_code",
                payload={"qrcode": refreshed.code, "qrcode_img_content": refreshed.image_content, "base_url": active_base_url, "refresh_count": refresh_count},
            )
        elif status_data.status == "scaned_but_redirect":
            if status_data.redirect_host:
                active_base_url = f"https://{status_data.redirect_host}"
                yield QRLoginEvent(type="redirect", payload={"base_url": active_base_url})

        time.sleep(poll_interval_seconds)
