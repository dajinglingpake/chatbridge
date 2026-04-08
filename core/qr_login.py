from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Iterator


DEFAULT_HEADERS = {
    "AuthorizationType": "ilink_bot_token",
    "iLink-App-Id": "bot",
    "iLink-App-ClientVersion": "131073",
}


@dataclass
class QRCodePayload:
    code: str
    image_content: str


@dataclass
class QRLoginEvent:
    type: str
    payload: dict


def _read_json(url: str, headers: dict[str, str], timeout: int) -> dict:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_bot_qr_code(base_url: str, headers: dict[str, str] | None = None, timeout: int = 15) -> QRCodePayload:
    payload = _read_json(f"{base_url}/ilink/bot/get_bot_qrcode?bot_type=3", headers or DEFAULT_HEADERS, timeout)
    return QRCodePayload(
        code=str(payload.get("qrcode") or "").strip(),
        image_content=str(payload.get("qrcode_img_content") or "").strip(),
    )


def fetch_qr_status(base_url: str, qr_code: str, headers: dict[str, str] | None = None, timeout: int = 60) -> dict:
    encoded = urllib.parse.quote(qr_code)
    return _read_json(f"{base_url}/ilink/bot/get_qrcode_status?qrcode={encoded}", headers or DEFAULT_HEADERS, timeout)


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
        status = str(status_data.get("status") or "").strip()
        if logger:
            logger(f"poll response: status={status}")

        if status == "confirmed":
            yield QRLoginEvent(type="confirmed", payload=status_data)
            return

        if status == "scaned":
            if not scanned:
                scanned = True
                yield QRLoginEvent(type="scanned", payload=status_data)
        elif status == "expired":
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
        elif status == "scaned_but_redirect":
            redirect_host = str(status_data.get("redirect_host") or "").strip()
            if redirect_host:
                active_base_url = f"https://{redirect_host}"
                yield QRLoginEvent(type="redirect", payload={"base_url": active_base_url})

        time.sleep(poll_interval_seconds)
