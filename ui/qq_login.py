from __future__ import annotations

import io
import queue
import threading
import time
import urllib.parse
from types import SimpleNamespace
from typing import Callable

import qrcode
import qrcode.image.svg

from core.app_service import run_named_action
from core.onebot_runtime_installer import fetch_napcat_login_qrcode_url
from runtime_stack import get_qq_login_status, get_runtime_snapshot


Translator = Callable[..., str]


def _tr(t: Translator, key: str, fallback: str, **kwargs: object) -> str:
    value = t(key, **kwargs)
    return value if value != key else fallback.format(**kwargs)


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
    on_success: Callable[[], None] | None = None,
) -> Callable[[], None]:
    def open_qq_login_dialog() -> None:
        dialog = ui.dialog().props("persistent")
        with dialog, ui.card().classes("cb-card cb-hero w-[30rem] max-w-[calc(100vw-1rem)] p-6"):
            with ui.column().classes("w-full gap-3"):
                ui.label("QQ Login").classes("cb-kicker")
                ui.label(_tr(t, "ui.qq_login.title", "扫码登录 QQ")).classes("text-2xl font-black text-white")
                status = ui.label(_tr(t, "ui.qq_login.idle", "QQ 登录组件未启动")).classes("cb-chip cb-chip-warn w-fit")
                with ui.element("div").classes("cb-panel w-full min-w-0 p-4 flex justify-center"):
                    qr_image = ui.image("").classes("w-full max-w-72 aspect-square h-auto self-center")
                    placeholder = ui.label(_tr(t, "ui.qq_login.click_get_qr", "点击获取二维码后启动 QQ 登录组件。")).classes("text-sm text-slate-300 self-center")
                detail = ui.label(_tr(t, "ui.qq_login.start_hint", "启动 UI 不会启动 QQ；需要时请手动获取二维码。")).classes("text-sm text-slate-300")
                with ui.row().classes("gap-2 flex-wrap"):
                    get_qr_button = ui.button(_tr(t, "ui.qq_login.get_qr", "获取二维码"), icon="qr_code_2")
                    close_button = ui.button(_tr(t, "ui.web.action.close", "关闭"), icon="close").props("outline color=white")

        event_queue: queue.SimpleQueue[tuple[str, str]] = queue.SimpleQueue()
        qr_visible = {"value": False}
        retry_mode = {"value": False}
        auto_close_enabled = {"value": False}
        login_notified = {"value": False}
        last_login_poll = {"value": 0.0}
        worker_running = {"value": False}
        login_done = {"value": False}

        def close_dialog() -> None:
            poll_timer.deactivate()
            dialog.close()

        def refresh_after_success() -> None:
            if on_success is not None:
                on_success()

        def show_qr_url(qr_url: str) -> None:
            qr_image.set_source(_qr_url_data_uri(qr_url))
            placeholder.visible = False
            status.text = _tr(t, "ui.qq_login.scan", "请使用手机 QQ 扫码")
            status.classes(replace="cb-chip cb-chip-warn w-fit")
            detail.text = _tr(t, "ui.qq_login.scan_hint", "请使用手机 QQ 扫码并确认登录。")
            qr_visible["value"] = True
            retry_mode["value"] = True

        def show_login_success(snapshot) -> bool:
            if not snapshot.qq_logged_in:
                return False
            account = f"{snapshot.qq_nickname} ({snapshot.qq_user_id})" if snapshot.qq_nickname and snapshot.qq_user_id else snapshot.qq_user_id or "-"
            status.text = _tr(t, "ui.qq_login.success", "QQ 登录成功")
            status.classes(replace="cb-chip cb-chip-ok w-fit")
            placeholder.visible = False
            detail.text = _tr(t, "ui.qq_login.logged_in", "当前 QQ：{account}", account=account)
            if not login_notified["value"]:
                login_notified["value"] = True
                notify(_tr(t, "ui.qq_login.success", "QQ 登录成功"))
            return True

        def finish_login_success(snapshot) -> None:
            if login_done["value"] or not show_login_success(snapshot):
                return
            login_done["value"] = True
            close_dialog()
            refresh_after_success()

        def current_login_snapshot():
            logged_in, user_id, nickname = get_qq_login_status()
            return SimpleNamespace(qq_logged_in=logged_in, qq_user_id=user_id, qq_nickname=nickname)

        def resolve_qr_action(*, force_restart: bool) -> str:
            if force_restart:
                return "restart-onebot-runtime"
            login_snapshot = current_login_snapshot()
            if login_snapshot.qq_logged_in:
                return "prepare-qq-login"
            runtime_snapshot = get_runtime_snapshot(include_agent_processes=False)
            return "restart-onebot-runtime" if runtime_snapshot.onebot_runtime_running else "prepare-qq-login"

        def start_worker(*, force_restart: bool = False) -> None:
            if worker_running["value"]:
                return
            worker_running["value"] = True
            auto_close_enabled["value"] = not current_login_snapshot().qq_logged_in
            get_qr_button.set_enabled(False)
            qr_visible["value"] = False
            qr_image.set_source("")
            status.text = _tr(t, "ui.qq_login.loading_qr", "正在获取二维码...")
            status.classes(replace="cb-chip cb-chip-warn w-fit")
            detail.text = _tr(t, "ui.qq_login.scan_hint", "请使用手机 QQ 扫码并确认登录。")
            placeholder.visible = True

            def worker() -> None:
                result = run_named_action(resolve_qr_action(force_restart=force_restart))
                deadline = time.monotonic() + 25.0
                last_error = ""
                should_refresh_qr = True
                while time.monotonic() < deadline:
                    if current_login_snapshot().qq_logged_in:
                        event_queue.put(("login_success", ""))
                        return
                    try:
                        qr_url = fetch_napcat_login_qrcode_url(refresh=should_refresh_qr, timeout=3.0)
                        if qr_url:
                            should_refresh_qr = False
                    except Exception as exc:  # noqa: BLE001
                        last_error = f"{type(exc).__name__}: {exc}"
                        time.sleep(1.0)
                        continue
                    if qr_url:
                        event_queue.put(("qr", qr_url))
                        return
                    time.sleep(1.0)
                event_queue.put(("error", last_error or result.message))

            threading.Thread(target=worker, daemon=True).start()

        def drain_events() -> None:
            while True:
                try:
                    event_type, message = event_queue.get_nowait()
                except queue.Empty:
                    break
                worker_running["value"] = False
                get_qr_button.set_enabled(True)
                get_qr_button.text = _tr(t, "ui.web.action.reload_qr", "重新获取二维码")
                retry_mode["value"] = True
                if event_type == "qr":
                    show_qr_url(message)
                    notify(_tr(t, "ui.qq_login.qr_ready", "QQ 登录二维码已生成"))
                    continue
                if event_type == "error":
                    status.text = _tr(t, "ui.qq_login.qr_failed", "二维码获取失败")
                    status.classes(replace="cb-chip cb-chip-danger w-fit")
                    detail.text = message
                    continue
                if event_type == "login_success":
                    snapshot = current_login_snapshot()
                    if auto_close_enabled["value"]:
                        finish_login_success(snapshot)
                        return
                    show_login_success(snapshot)
                    continue
                status.text = _tr(t, "ui.qq_login.loading_qr", "正在获取二维码...")
                status.classes(replace="cb-chip cb-chip-warn w-fit")
                detail.text = _tr(t, "ui.qq_login.waiting_runtime_qr", "QQ 登录组件已启动，正在等待二维码生成。", message=message)
                if show_login_success(get_runtime_snapshot(include_agent_processes=False)):
                    finish_login_success(current_login_snapshot())
                    return

            if not auto_close_enabled["value"] or not qr_visible["value"] or time.monotonic() - last_login_poll["value"] < 2.0:
                return
            last_login_poll["value"] = time.monotonic()
            finish_login_success(current_login_snapshot())

        get_qr_button.on_click(lambda: start_worker(force_restart=retry_mode["value"]))
        close_button.on_click(close_dialog)
        poll_timer = ui.timer(0.5, drain_events)
        dialog.open()
        if show_login_success(current_login_snapshot()):
            get_qr_button.text = _tr(t, "ui.web.action.reload_qr", "重新获取二维码")
            retry_mode["value"] = True

    return open_qq_login_dialog
