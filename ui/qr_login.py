from __future__ import annotations

import threading
from pathlib import Path

from core.accounts import resolve_ilink_base_url, save_account_from_qr_payload
from core.qr_login import iter_qr_login_events


def install_qr_login_dialog(ui, notify, refresh_view) -> callable:
    def open_qr_login_dialog() -> None:
        dialog = ui.dialog()
        with dialog, ui.card().classes("w-[28rem] max-w-full"):
            ui.label("扫码登录微信").classes("text-xl font-semibold")
            status = ui.label("正在获取二维码...").classes("text-slate-600")
            qr_image = ui.image("").classes("w-72 h-72 self-center")
            hint = ui.label("请使用微信扫码并在手机上确认授权。").classes("text-sm text-slate-500")
            ui.button("关闭", on_click=dialog.close).props("flat")

        def worker() -> None:
            try:
                for event in iter_qr_login_events(resolve_ilink_base_url()):
                    ui.context.client.connected()  # keep NiceGUI context alive
                    if event.type == "qr_code":
                        qr_url = str(event.payload.get("qrcode_img_content") or "")
                        status.text = "请使用微信扫码"
                        qr_image.set_source(qr_url)
                        hint.text = "扫码后请在手机上确认授权。"
                    elif event.type == "scanned":
                        status.text = "已扫码，等待手机确认"
                        hint.text = "请在手机上完成确认。"
                    elif event.type == "redirect":
                        hint.text = f"登录节点切换到 {event.payload.get('base_url')}"
                    elif event.type == "expired":
                        status.text = "二维码已过期"
                        hint.text = "请关闭后重新打开扫码登录。"
                        notify("二维码已过期，请重新打开扫码登录。")
                        return
                    elif event.type == "confirmed":
                        saved = save_account_from_qr_payload(event.payload, resolve_ilink_base_url())
                        if saved is None:
                            status.text = "账号保存失败"
                            hint.text = "返回 payload 缺少必要字段。"
                            notify("扫码成功，但账号保存失败。")
                            return
                        status.text = "扫码成功"
                        hint.text = f"已保存账号：{saved.account_id}"
                        notify(f"已保存账号：{saved.account_id}")
                        refresh_view()
                        dialog.close()
                        return
                    elif event.type == "error":
                        status.text = "二维码登录失败"
                        hint.text = str(event.payload.get("message") or "未知错误")
                        notify(f"二维码登录失败：{hint.text}")
                        return
            except Exception as exc:  # noqa: BLE001
                status.text = "二维码登录失败"
                hint.text = str(exc)
                notify(f"二维码登录失败：{exc}")

        threading.Thread(target=worker, daemon=True).start()
        dialog.open()

    return open_qr_login_dialog
