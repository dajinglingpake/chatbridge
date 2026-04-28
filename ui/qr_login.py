from __future__ import annotations

import base64
import binascii
import io
import queue
import threading
import urllib.parse
from typing import Callable

import qrcode
import qrcode.image.svg

from bridge_config import BridgeConfig
from core.accounts import resolve_ilink_base_url, save_account_from_qr_payload
from core.qr_login import QRLoginEvent, iter_qr_login_events
from core.weixin_notifier import broadcast_weixin_notice_by_kind

Translator = Callable[..., str]


def _tr(t: Translator, key: str, fallback: str, **kwargs: object) -> str:
    value = t(key, **kwargs)
    return value if value != key else fallback.format(**kwargs)


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


def _normalize_qr_image_source(content: object) -> str:
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


def _build_qr_data_uri(content: object) -> str:
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


def install_qr_login_dialog(
    ui,
    notify: Callable[[str], None],
    refresh_view: Callable[[], None],
    t: Translator,
    on_open: Callable[[], None] | None = None,
    on_close: Callable[[], None] | None = None,
) -> Callable[[], None]:
    def open_qr_login_dialog() -> None:
        if on_open is not None:
            on_open()
        dialog = ui.dialog().props("persistent")
        with dialog, ui.card().classes("cb-card cb-hero w-[30rem] max-w-full p-6"):
            with ui.column().classes("w-full gap-3"):
                ui.label("WeChat Login").classes("cb-kicker")
                ui.label(_tr(t, "ui.qr.title", "扫码登录微信")).classes("text-2xl font-black text-white")
                status = ui.label(_tr(t, "ui.qr.loading", "正在获取二维码...")).classes("cb-chip cb-chip-warn w-fit")
                with ui.element("div").classes("cb-panel w-full p-4 flex justify-center"):
                    qr_image = ui.image("").classes("w-72 h-72 self-center")
                hint = ui.label(_tr(t, "ui.qr.initial_hint", "请使用微信扫码并在手机上确认授权。")).classes("text-sm text-slate-300")
                with ui.row().classes("gap-2 flex-wrap"):
                    retry_button = ui.button(_tr(t, "ui.web.action.reload_qr", "重新获取二维码"), icon="refresh").props("outline color=white")
                    close_button = ui.button(_tr(t, "ui.web.action.close", "关闭"), icon="close").props("outline color=white")

        event_queue: queue.SimpleQueue[tuple[str, QRLoginEvent | Exception]] = queue.SimpleQueue()
        stop_event = threading.Event()

        def stop_polling() -> None:
            stop_event.set()
            poll_timer.deactivate()

        def close_dialog() -> None:
            stop_polling()
            dialog.close()
            if on_close is not None:
                on_close()

        def retry_login() -> None:
            close_dialog()
            open_qr_login_dialog()

        close_button.on_click(close_dialog)
        retry_button.on_click(retry_login)

        def apply_login_event(event: QRLoginEvent) -> None:
            if event.type == "qr_code":
                qr_source = _build_qr_data_uri(event.image_content)
                if not qr_source:
                    status.text = _tr(t, "ui.qr.failed", "二维码登录失败")
                    status.classes(replace="cb-chip cb-chip-danger w-fit")
                    hint.text = _tr(t, "ui.qr.invalid_image", "二维码图片内容为空或格式不受支持。")
                    notify(_tr(t, "ui.qr.invalid_image_notice", "二维码登录失败：二维码图片内容为空或格式不受支持。"))
                    return
                status.text = _tr(t, "ui.qr.scan", "请使用微信扫码")
                status.classes(replace="cb-chip cb-chip-warn w-fit")
                qr_image.set_source(qr_source)
                hint.text = _tr(t, "ui.qr.scan_hint", "扫码后请在手机上确认授权。")
                return

            if event.type == "scanned":
                status.text = _tr(t, "ui.qr.scanned", "已扫码，等待手机确认")
                status.classes(replace="cb-chip cb-chip-warn w-fit")
                hint.text = _tr(t, "ui.qr.confirm_hint", "请在手机上完成确认。")
                return

            if event.type == "redirect":
                hint.text = _tr(t, "ui.qr.redirect", "登录节点切换到 {base_url}", base_url=event.base_url)
                return

            if event.type == "expired":
                status.text = _tr(t, "ui.qr.expired", "二维码已过期")
                status.classes(replace="cb-chip cb-chip-danger w-fit")
                hint.text = _tr(t, "ui.qr.expired_hint", "二维码已过期，请重新获取。")
                notify(_tr(t, "ui.qr.expired_notice", "二维码已过期，请重新获取。"))
                stop_polling()
                return

            if event.type == "confirmed":
                config = BridgeConfig.load()
                saved = save_account_from_qr_payload(
                    event.payload,
                    resolve_ilink_base_url(config),
                    config=config,
                )
                if saved is None:
                    status.text = _tr(t, "ui.qr.save_failed", "账号保存失败")
                    status.classes(replace="cb-chip cb-chip-danger w-fit")
                    hint.text = _tr(t, "ui.qr.missing_payload", "返回 payload 缺少必要字段。")
                    notify(_tr(t, "ui.qr.save_failed_notice", "扫码成功，但账号保存失败。"))
                    return
                status.text = _tr(t, "ui.qr.success", "扫码成功")
                status.classes(replace="cb-chip cb-chip-ok w-fit")
                hint.text = _tr(
                    t,
                    "ui.qr.saved_wait_context",
                    "已保存并切换账号：{account}。请在新微信会话中发送第一条消息后建立通知通道。",
                    account=saved.account_id,
                )
                notice_result = broadcast_weixin_notice_by_kind(
                    "config",
                    _tr(t, "ui.qr.notice_title", "微信账号已切换"),
                    _tr(t, "ui.qr.notice_detail", "当前账号：{account}", account=saved.account_id),
                    config=config,
                )
                notify(
                    _tr(
                        t,
                        "ui.qr.saved_notice_result",
                        "已保存并切换账号：{account}；微信通知：{summary}",
                        account=saved.account_id,
                        summary=notice_result.summary,
                    )
                )
                refresh_view()
                stop_polling()
                return

            if event.type == "error":
                status.text = _tr(t, "ui.qr.failed", "二维码登录失败")
                status.classes(replace="cb-chip cb-chip-danger w-fit")
                hint.text = event.message or _tr(t, "ui.qr.unknown_error", "未知错误")
                notify(_tr(t, "ui.qr.failed_with_reason", "二维码登录失败：{reason}", reason=hint.text))
                stop_polling()

        def drain_events() -> None:
            while True:
                try:
                    event_kind, payload = event_queue.get_nowait()
                except queue.Empty:
                    return

                if event_kind == "event":
                    if isinstance(payload, QRLoginEvent):
                        apply_login_event(payload)
                    continue

                status.text = _tr(t, "ui.qr.failed", "二维码登录失败")
                status.classes(replace="cb-chip cb-chip-danger w-fit")
                hint.text = str(payload)
                notify(_tr(t, "ui.qr.failed_with_reason", "二维码登录失败：{reason}", reason=payload))
                stop_polling()
                return

        def worker() -> None:
            try:
                config = BridgeConfig.load()
                for event in iter_qr_login_events(resolve_ilink_base_url(config)):
                    if stop_event.is_set():
                        return
                    event_queue.put(("event", event))
            except Exception as exc:  # noqa: BLE001
                event_queue.put(("exception", exc))

        poll_timer = ui.timer(0.2, drain_events)
        threading.Thread(target=worker, daemon=True).start()
        dialog.open()

    return open_qr_login_dialog
