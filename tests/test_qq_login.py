from __future__ import annotations

import unittest
from unittest.mock import patch

from ui.qq_login import install_qq_login_dialog


class FakeElement:
    def __init__(self, kind: str, text: str = "", **attrs: object) -> None:
        self.kind = kind
        self.text = text
        self.attrs = attrs
        self.value = ""
        self.source = ""
        self.enabled = True

    def __enter__(self) -> "FakeElement":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def classes(self, value: str = "", **kwargs: object) -> "FakeElement":
        self.attrs["classes"] = kwargs.get("replace", value)
        return self

    def props(self, value: str) -> "FakeElement":
        self.attrs["props"] = value
        return self

    def set_enabled(self, value: bool) -> "FakeElement":
        self.enabled = value
        return self

    def set_source(self, value: str) -> "FakeElement":
        self.source = value
        return self

    def on_click(self, handler) -> "FakeElement":
        self.attrs["on_click"] = handler
        return self

    def open(self) -> None:
        self.attrs["open"] = True

    def close(self) -> None:
        self.attrs["closed"] = True

    def deactivate(self) -> None:
        self.attrs["active"] = False


class FakeThread:
    def __init__(self, target, daemon: bool = False) -> None:
        self.target = target
        self.daemon = daemon

    def start(self) -> None:
        self.target()


class FakeUI:
    def __init__(self) -> None:
        self.elements: list[FakeElement] = []
        self.timers: list[object] = []

    def _element(self, kind: str, text: str = "", **attrs: object) -> FakeElement:
        element = FakeElement(kind, text, **attrs)
        self.elements.append(element)
        return element

    def dialog(self) -> FakeElement:
        return self._element("dialog")

    def card(self) -> FakeElement:
        return self._element("card")

    def column(self) -> FakeElement:
        return self._element("column")

    def row(self) -> FakeElement:
        return self._element("row")

    def element(self, tag: str) -> FakeElement:
        return self._element(f"element:{tag}")

    def label(self, text: str = "") -> FakeElement:
        return self._element("label", text)

    def image(self, source: str) -> FakeElement:
        return self._element("image", source)

    def button(self, text: str, on_click=None, **kwargs) -> FakeElement:
        return self._element("button", text, on_click=on_click, **kwargs)

    def timer(self, _interval: float, callback) -> FakeElement:
        timer = self._element("timer", callback=callback)
        self.timers.append(timer)
        return timer


def _translator(key: str, **_kwargs: object) -> str:
    return key


class QQLoginDialogTests(unittest.TestCase):
    def test_opening_dialog_does_not_start_qq_stack(self) -> None:
        ui = FakeUI()
        dialog = install_qq_login_dialog(ui, lambda _message: None, _translator)

        with (
            patch("ui.qq_login.run_named_action") as mocked_run,
            patch("ui.qq_login.get_qq_login_status", return_value=(False, "", "")),
        ):
            dialog()

        mocked_run.assert_not_called()
        qr_buttons = [element.text for element in ui.elements if element.kind == "button" and "二维码" in element.text]
        self.assertEqual(["获取二维码"], qr_buttons)

    def test_get_qr_button_starts_qq_stack_explicitly(self) -> None:
        ui = FakeUI()
        notifications: list[str] = []
        dialog = install_qq_login_dialog(ui, notifications.append, _translator)

        with (
            patch("ui.qq_login.threading.Thread", FakeThread),
            patch("ui.qq_login.run_named_action") as mocked_run,
            patch("ui.qq_login.fetch_napcat_login_qrcode_url", return_value="https://example.test/qr"),
            patch("ui.qq_login.get_qq_login_status", return_value=(False, "", "")),
        ):
            mocked_run.return_value.message = "started"
            dialog()
            get_button = next(element for element in ui.elements if element.kind == "button" and element.text == "获取二维码")
            get_button.attrs["on_click"]()
            ui.timers[-1].attrs["callback"]()

        mocked_run.assert_called_once_with("prepare-qq-login")
        self.assertIn("QQ 登录二维码已生成", notifications)

    def test_opening_dialog_shows_existing_login_without_starting_stack(self) -> None:
        ui = FakeUI()
        dialog = install_qq_login_dialog(ui, lambda _message: None, _translator)

        with (
            patch("ui.qq_login.run_named_action") as mocked_run,
            patch("ui.qq_login.get_qq_login_status", return_value=(True, "12345", "Alice")),
        ):
            dialog()

        mocked_run.assert_not_called()
        self.assertTrue(any("当前 QQ" in element.text for element in ui.elements if element.kind == "label"))

    def test_login_success_closes_dialog_and_refreshes_status(self) -> None:
        ui = FakeUI()
        notifications: list[str] = []
        refreshed: list[bool] = []
        dialog = install_qq_login_dialog(ui, notifications.append, _translator, on_success=lambda: refreshed.append(True))

        with (
            patch("ui.qq_login.threading.Thread", FakeThread),
            patch("ui.qq_login.run_named_action") as mocked_run,
            patch("ui.qq_login.fetch_napcat_login_qrcode_url", return_value="https://example.test/qr"),
            patch("ui.qq_login.get_qq_login_status", side_effect=[(False, "", ""), (False, "", ""), (True, "12345", "Alice")]),
        ):
            mocked_run.return_value.message = "started"
            dialog()
            get_button = next(element for element in ui.elements if element.kind == "button" and element.text == "获取二维码")
            get_button.attrs["on_click"]()
            ui.timers[-1].attrs["callback"]()

        dialog_element = next(element for element in ui.elements if element.kind == "dialog")
        self.assertTrue(dialog_element.attrs.get("closed"))
        self.assertEqual([True], refreshed)
        self.assertIn("QQ 登录成功", notifications)

    def test_retry_qr_while_already_logged_in_does_not_close_dialog(self) -> None:
        ui = FakeUI()
        dialog = install_qq_login_dialog(ui, lambda _message: None, _translator)

        with (
            patch("ui.qq_login.threading.Thread", FakeThread),
            patch("ui.qq_login.run_named_action") as mocked_run,
            patch("ui.qq_login.fetch_napcat_login_qrcode_url", return_value="https://example.test/qr"),
            patch("ui.qq_login.get_qq_login_status", return_value=(True, "12345", "Alice")),
        ):
            mocked_run.return_value.message = "started"
            dialog()
            get_button = next(element for element in ui.elements if element.kind == "button" and element.text == "重新获取二维码")
            get_button.attrs["on_click"]()
            ui.timers[-1].attrs["callback"]()

        dialog_element = next(element for element in ui.elements if element.kind == "dialog")
        self.assertFalse(dialog_element.attrs.get("closed", False))
        mocked_run.assert_called_once_with("restart-onebot-runtime")


if __name__ == "__main__":
    unittest.main()
