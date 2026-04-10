from __future__ import annotations

import base64
import binascii
import io
import queue
import threading
import urllib.parse
from typing import Any

import qrcode
import qrcode.image.svg

from core.accounts import resolve_ilink_base_url, save_account_from_qr_payload
from core.qr_login import iter_qr_login_events


def _detect_image_mime(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _normalize_qr_image_source(content: Any) -> str:
    if content is None:
        return ""

    if isinstance(content, bytes):
        image_bytes = content
    else:
        text = str(content).strip()
        if not text:
            return ""
        if text.startswith(("http://", "https://", "data:image/")):
            return text

        compact = "".join(text.split())
        try:
            image_bytes = base64.b64decode(compact, validate=True)
        except (binascii.Error, ValueError):
            try:
                image_bytes = text.encode("latin1")
            except UnicodeEncodeError:
                return text

    if not image_bytes:
        return ""

    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{_detect_image_mime(image_bytes)};base64,{encoded}"


def _build_qr_data_uri(content: Any) -> str:
    if content is None:
        return ""

    if isinstance(content, bytes):
        return _normalize_qr_image_source(content)

    text = str(content).strip()
    if not text:
        return ""
    if text.startswith("data:image/"):
        return text

    qr = qrcode.QRCode(version=3, box_size=8, border=2)
    qr.add_data(text)
    qr.make(fit=True)
    image = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    buffer = io.BytesIO()
    image.save(buffer)
    svg = buffer.getvalue().decode("utf-8")
    return f"data:image/svg+xml;utf8,{urllib.parse.quote(svg)}"


def install_qr_login_dialog(ui, notify, refresh_view) -> callable:
    def open_qr_login_dialog() -> None:
        dialog = ui.dialog()
        with dialog, ui.card().classes("cb-card cb-hero w-[30rem] max-w-full p-6"):
            with ui.column().classes("w-full gap-3"):
                ui.label("WeChat Login").classes("cb-kicker")
                ui.label("扫码登录微信").classes("text-2xl font-black tracking-tight text-slate-900")
                status = ui.label("正在获取二维码...").classes("cb-chip cb-chip-warn w-fit")
                with ui.card().classes("cb-soft-card w-full p-4 shadow-none"):
                    qr_image = ui.image("").classes("w-72 h-72 self-center")
                hint = ui.label("请使用微信扫码并在手机上确认授权。").classes("text-sm cb-muted")
                close_button = ui.button("关闭").props("outline")

        event_queue: queue.SimpleQueue[tuple[str, Any]] = queue.SimpleQueue()
        stop_event = threading.Event()

        def close_dialog() -> None:
            stop_event.set()
            poll_timer.deactivate()
            dialog.close()

        close_button.on_click(close_dialog)

        def apply_login_event(event: Any) -> None:
            if event.type == "qr_code":
                qr_source = _build_qr_data_uri(event.payload.get("qrcode_img_content"))
                if not qr_source:
                    status.text = "二维码登录失败"
                    status.classes(replace="cb-chip cb-chip-danger w-fit")
                    hint.text = "二维码图片内容为空或格式不受支持。"
                    notify("二维码登录失败：二维码图片内容为空或格式不受支持。")
                    return
                status.text = "请使用微信扫码"
                status.classes(replace="cb-chip cb-chip-warn w-fit")
                qr_image.set_source(qr_source)
                hint.text = "扫码后请在手机上确认授权。"
                return

            if event.type == "scanned":
                status.text = "已扫码，等待手机确认"
                status.classes(replace="cb-chip cb-chip-warn w-fit")
                hint.text = "请在手机上完成确认。"
                return

            if event.type == "redirect":
                hint.text = f"登录节点切换到 {event.payload.get('base_url')}"
                return

            if event.type == "expired":
                status.text = "二维码已过期"
                status.classes(replace="cb-chip cb-chip-danger w-fit")
                hint.text = "请关闭后重新打开扫码登录。"
                notify("二维码已过期，请重新打开扫码登录。")
                close_dialog()
                return

            if event.type == "confirmed":
                saved = save_account_from_qr_payload(event.payload, resolve_ilink_base_url())
                if saved is None:
                    status.text = "账号保存失败"
                    status.classes(replace="cb-chip cb-chip-danger w-fit")
                    hint.text = "返回 payload 缺少必要字段。"
                    notify("扫码成功，但账号保存失败。")
                    return
                status.text = "扫码成功"
                status.classes(replace="cb-chip cb-chip-ok w-fit")
                hint.text = f"已保存账号：{saved.account_id}"
                notify(f"已保存账号：{saved.account_id}")
                refresh_view()
                close_dialog()
                return

            if event.type == "error":
                status.text = "二维码登录失败"
                status.classes(replace="cb-chip cb-chip-danger w-fit")
                hint.text = str(event.payload.get("message") or "未知错误")
                notify(f"二维码登录失败：{hint.text}")

        def drain_events() -> None:
            while True:
                try:
                    event_kind, payload = event_queue.get_nowait()
                except queue.Empty:
                    return

                if event_kind == "event":
                    apply_login_event(payload)
                    continue

                status.text = "二维码登录失败"
                status.classes(replace="cb-chip cb-chip-danger w-fit")
                hint.text = str(payload)
                notify(f"二维码登录失败：{payload}")
                close_dialog()
                return

        def worker() -> None:
            try:
                for event in iter_qr_login_events(resolve_ilink_base_url()):
                    if stop_event.is_set():
                        return
                    event_queue.put(("event", event))
            except Exception as exc:  # noqa: BLE001
                event_queue.put(("exception", exc))

        poll_timer = ui.timer(0.2, drain_events)
        threading.Thread(target=worker, daemon=True).start()
        dialog.open()

    return open_qr_login_dialog
