from __future__ import annotations

import threading
from collections.abc import Callable

from PySide6.QtCore import QMetaObject, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from core.qr_login import iter_qr_login_events


def show_qr_login_dialog(
    parent: QWidget,
    t: Callable[..., str],
    base_url_provider: Callable[[], str],
    qr_pixmap_factory: Callable[[str], QPixmap],
    save_account_callback: Callable[[dict], None],
    logger: Callable[[str], None],
) -> None:
    dialog = QDialog(parent)
    dialog.setWindowTitle(t("ui.account.qr_login.title"))
    dialog.setMinimumSize(400, 520)
    layout = QVBoxLayout(dialog)

    status_label = QLabel(t("ui.account.qr_login.getting"))
    status_label.setAlignment(Qt.AlignCenter)
    layout.addWidget(status_label)

    qr_label = QLabel()
    qr_label.setAlignment(Qt.AlignCenter)
    qr_label.setMinimumSize(300, 300)
    layout.addWidget(qr_label)

    hint_label = QLabel(t("ui.account.qr_login.hint"))
    hint_label.setAlignment(Qt.AlignCenter)
    hint_label.setWordWrap(True)
    layout.addWidget(hint_label)

    btn_layout = QHBoxLayout()
    cancel_btn = QPushButton(t("ui.button.cancel"))
    cancel_btn.clicked.connect(dialog.reject)
    btn_layout.addWidget(cancel_btn)
    btn_layout.addStretch()
    layout.addLayout(btn_layout)

    def do_update() -> None:
        base_url = base_url_provider()
        logger(f"fetching QR from {base_url}")
        try:
            for event in iter_qr_login_events(base_url, logger=logger):
                if event.type == "qr_code":
                    qr_url = str(event.payload.get("qrcode_img_content") or "")
                    if not qr_url:
                        status_label.setText(t("ui.account.qr_login.error"))
                        return
                    pixmap = qr_pixmap_factory(qr_url)
                    logger(f"Pixmap generated: {pixmap.width()}x{pixmap.height()}")
                    qr_label.setPixmap(pixmap)
                    status_label.setText(t("ui.account.qr_login.scan"))
                    continue
                if event.type == "scanned":
                    hint_label.setText(t("ui.account.qr_login.confirming"))
                    continue
                if event.type == "redirect":
                    logger(f"IDC redirect to {event.payload.get('base_url')}")
                    continue
                if event.type == "expired":
                    status_label.setText(t("ui.account.qr_login.expired"))
                    hint_label.setText(t("ui.account.qr_login.retry"))
                    return
                if event.type == "confirmed":
                    status_label.setText(t("ui.account.qr_login.success"))
                    hint_label.setText(t("ui.account.qr_login.saving"))
                    logger("Status confirmed, saving account...")
                    save_account_callback(event.payload)
                    logger("Account saved, closing dialog...")
                    QMetaObject.invokeMethod(dialog, "accept", Qt.QueuedConnection)
                    logger("Dialog close requested")
                    return
                if event.type == "error":
                    status_label.setText(t("ui.account.qr_login.error"))
                    return
        except Exception as exc:  # noqa: BLE001
            logger(f"fetch error: {exc}")
            status_label.setText(t("ui.account.qr_login.error_detail", error=str(exc)))

    threading.Thread(target=do_update, daemon=True).start()
    dialog.exec()
