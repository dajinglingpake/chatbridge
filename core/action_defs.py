from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ActionDefinition:
    key: str
    label: str


REFRESH_ACTION = ActionDefinition(key="refresh", label="重新检测")
LOGIN_ACTION = ActionDefinition(key="login", label="扫码登录微信")
SESSIONS_ACTION = ActionDefinition(key="sessions", label="查看会话")
DIAGNOSTICS_ACTION = ActionDefinition(key="diagnostics", label="诊断与日志")
AUTO_REFRESH_ON_ACTION = ActionDefinition(key="auto_refresh_on", label="自动刷新：开")
AUTO_REFRESH_OFF_ACTION = ActionDefinition(key="auto_refresh_off", label="自动刷新：关")

TOPBAR_ACTIONS: tuple[ActionDefinition, ...] = (
    REFRESH_ACTION,
    LOGIN_ACTION,
    SESSIONS_ACTION,
    DIAGNOSTICS_ACTION,
)
