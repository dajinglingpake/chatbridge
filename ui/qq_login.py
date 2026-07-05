from __future__ import annotations

import base64
import io
import queue
import threading
import time
import urllib.parse
from typing import Callable

import qrcode
import qrcode.image.svg

from core.app_service import run_named_action
from core.onebot_runtime_installer import fetch_napcat_login_qrcode_url, find_latest_qr_image
from runtime_stack import get_runtime_snapshot


Translator = Callable[..., str]


def _tr(t: Translator, key: str, fallback: str, **kwargs: object) -> str:
    value = t(key, **kwargs)
    return value if value != key else fallback.format(**kwargs)


def _image_data_uri(image_bytes: bytes) -> str:
    return f"data:image/png;base64,{base64.b64encode(image_bytes).decode('ascii')}"


def _qr_url_data_uri(qr_url: str) -> str:
    qr = qrcode.QRCode(version=3, box_size=8, border=2)
    qr.add_data(qr_url)
    qr.make(fit=True)
    image = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    buffer = io.BytesIO()
    image.save(buffer)
    svg = buffer.getvalue().decode("utf-8")
    return f"data:image/svg+xml;utf8,{urllib.parse.quote(svg)}"


def install_qq_login_dialog(
    ui,
    notify: Callable[[str], None],
    t: Translator,
) -> Callable[[], None]:
    def open_qq_login_dialog() -> None:
        dialog = ui.dialog().props("persistent")
        with dialog, ui.card().classes("cb-card cb-hero w-[30rem] max-w-[calc(100vw-1rem)] p-6"):
            with ui.column().classes("w-full gap-3"):
                ui.label("QQ Login").classes("cb-kicker")
                ui.label(_tr(t, "ui.qq_login.title", "扫码登录 QQ")).classes("text-2xl font-black text-white")
                status = ui.label(_tr(t, "ui.qq_login.preparing", "正在准备 QQ 登录组件...")).classes("cb-chip cb-chip-warn w-fit")
                with ui.element("div").classes("cb-panel w-full min-w-0 p-4 flex justify-center"):
                    qr_image = ui.image("").classes("w-full max-w-72 aspect-square h-auto self-center")
                    placeholder = ui.label(_tr(t, "ui.qq_login.waiting_qr", "正在获取二维码...")).classes("text-sm text-slate-300 self-center")
                detail = ui.label(_tr(t, "ui.qq_login.scan_hint", "请使用手机 QQ 扫码并确认登录。")).classes("text-sm text-slate-300")
                with ui.row().classes("gap-2 flex-wrap"):
                    retry_button = ui.button(_tr(t, "ui.web.action.reload_qr", "重新获取二维码"), icon="refresh").props("outline color=white")
                    close_button = ui.button(_tr(t, "ui.web.action.close", "关闭"), icon="close").props("outline color=white")

        event_queue: queue.SimpleQueue[tuple[str, str]] = queue.SimpleQueue()
        started_at = {"value": 0.0}
        qr_since = {"value": time.time()}
        restart_count = {"value": 0}

        def close_dialog() -> None:
            poll_timer.deactivate()
            dialog.close()

        def update_qr_image() -> bool:
            try:
                qr_url = fetch_napcat_login_qrcode_url(refresh=False, timeout=1.0)
            except Exception:  # noqa: BLE001
                qr_url = ""
            if qr_url:
                qr_image.set_source(_qr_url_data_uri(qr_url))
                placeholder.visible = False
                status.text = _tr(t, "ui.qq_login.scan", "请使用手机 QQ 扫码")
                status.classes(replace="cb-chip cb-chip-warn w-fit")
                detail.text = _tr(t, "ui.qq_login.scan_hint", "请使用手机 QQ 扫码并确认登录。")
                return True

            qr_path = find_latest_qr_image(since=qr_since["value"]) or find_latest_qr_image()
            if qr_path is None:
                return False
            try:
                qr_image.set_source(_image_data_uri(qr_path.read_bytes()))
            except OSError as exc:
                detail.text = _tr(t, "ui.qq_login.qr_load_failed", "二维码图片读取失败：{reason}", reason=exc)
                return False
            placeholder.visible = False
            status.text = _tr(t, "ui.qq_login.scan", "请使用手机 QQ 扫码")
            status.classes(replace="cb-chip cb-chip-warn w-fit")
            detail.text = _tr(t, "ui.qq_login.scan_hint", "扫码后请在手机 QQ 中确认登录。登录成功后即可通过 QQ 私聊或群聊发送消息。")
            return True

        def start_worker() -> None:
            started_at["value"] = time.monotonic()
            qr_since["value"] = time.time() - 1.0
            status.text = _tr(t, "ui.qq_login.loading_qr", "正在获取二维码...")
            status.classes(replace="cb-chip cb-chip-warn w-fit")
            detail.text = _tr(t, "ui.qq_login.scan_hint", "请使用手机 QQ 扫码并确认登录。")
            placeholder.visible = True

            def worker() -> None:
                result = run_named_action("prepare-qq-login")
                try:
                    fetch_napcat_login_qrcode_url(refresh=True, timeout=2.0)
                except Exception:  # noqa: BLE001
                    pass
                event_queue.put(("result", result.message))

            threading.Thread(target=worker, daemon=True).start()

        def drain_events() -> None:
            has_qr = update_qr_image()
            if not has_qr and started_at["value"] and time.monotonic() - started_at["value"] > 12 and restart_count["value"] < 1:
                restart_count["value"] += 1
                start_worker()
                return
            while True:
                try:
                    event_type, message = event_queue.get_nowait()
                except queue.Empty:
                    return
                if event_type != "result":
                    continue
                if update_qr_image():
                    notify(_tr(t, "ui.qq_login.qr_ready", "QQ 登录二维码已生成"))
                    continue
                snapshot = get_runtime_snapshot(include_agent_processes=False)
                if not snapshot.onebot_runtime_running:
                    status.text = _tr(t, "ui.qq_login.loading_qr", "正在获取二维码...")
                    status.classes(replace="cb-chip cb-chip-warn w-fit")
                    detail.text = _tr(
                        t,
                        "ui.qq_login.retrying_hint",
                        "QQ 登录组件正在自动重试，稍等片刻即可。",
                        message=message,
                    )
                    continue
                status.text = _tr(t, "ui.qq_login.loading_qr", "正在获取二维码...")
                status.classes(replace="cb-chip cb-chip-warn w-fit")
                detail.text = _tr(
                    t,
                    "ui.qq_login.waiting_runtime_qr",
                    "QQ 登录组件已启动，正在等待二维码生成。",
                    message=message,
                )

        retry_button.on_click(start_worker)
        close_button.on_click(close_dialog)
        poll_timer = ui.timer(0.5, drain_events)
        notify(_tr(t, "ui.qq_login.opened", "正在准备 QQ 登录"))
        dialog.open()
        start_worker()

    return open_qq_login_dialog
