from __future__ import annotations

from collections.abc import Callable

from core.app_service import run_named_action


Notify = Callable[[str], None]
Jump = Callable[[str], None]
Refresh = Callable[[], None]
OpenQRLogin = Callable[[], None]


def execute_topbar_action(action_key: str, *, refresh: Refresh, jump: Jump, notify: Notify, open_qr_login: OpenQRLogin) -> None:
    handlers: dict[str, Callable[[], None]] = {
        "refresh": refresh,
        "login": open_qr_login,
        "sessions": lambda: jump("sessions"),
        "diagnostics": lambda: jump("diagnostics"),
    }
    handler = handlers.get(action_key)
    if handler is None:
        notify(f"未支持的顶部动作：{action_key}")
        return
    handler()


def execute_primary_action(action_key: str, *, refresh: Refresh, jump: Jump, notify: Notify, open_qr_login: OpenQRLogin) -> None:
    runtime_actions = {"start", "stop"}
    if action_key in runtime_actions:
        notify(run_named_action(action_key).message)
        refresh()
        return
    if action_key == "repair":
        _jump_with_notice(jump, notify, "issues", "请先查看异常区中的修复建议，自动修复流程后续接入。")
        return
    if action_key == "login":
        open_qr_login()
        return
    refresh()


def _jump_with_notice(jump: Jump, notify: Notify, anchor: str, message: str) -> None:
    jump(anchor)
    notify(message)
