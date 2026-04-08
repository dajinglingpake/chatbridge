from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QInputDialog, QWidget

from core.accounts import AccountOption


def select_account_option(
    parent: QWidget,
    t: Callable[..., str],
    options: list[AccountOption],
    current_index: int,
) -> AccountOption | None:
    labels = [item.text for item in options]
    selected_text, accepted = QInputDialog.getItem(
        parent,
        t("ui.account.dialog.title"),
        t("ui.account.dialog.label"),
        labels,
        current_index,
        False,
    )
    if not accepted or not selected_text:
        return None
    for option in options:
        if option.text == selected_text:
            return option
    return None
