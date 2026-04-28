from __future__ import annotations

from collections.abc import Callable

Notify = Callable[[str], None]
Jump = Callable[[str], None]
Refresh = Callable[[], None]
OpenQRLogin = Callable[[], None]
Translate = Callable[..., str]


def _t(translate: Translate | None, key: str, fallback: str, **kwargs: object) -> str:
    if translate is None:
        return fallback.format(**kwargs)
    value = translate(key, **kwargs)
    return value if value != key else fallback.format(**kwargs)


def execute_topbar_action(action_key: str, *, refresh: Refresh, jump: Jump, notify: Notify, open_qr_login: OpenQRLogin, translate: Translate | None = None) -> None:
    handlers: dict[str, Callable[[], None]] = {
        "refresh": refresh,
        "login": open_qr_login,
        "sessions": lambda: jump("sessions"),
        "diagnostics": lambda: jump("diagnostics"),
    }
    handler = handlers.get(action_key)
    if handler is None:
        notify(_t(translate, "ui.web.notify.unsupported_topbar_action", "未支持的顶部动作：{action}", action=action_key))
        return
    handler()
