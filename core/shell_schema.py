from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from core.action_defs import ActionDefinition, TOPBAR_ACTIONS
from core.navigation import PRIMARY_PAGES
from core.navigation import PageDefinition


@dataclass(frozen=True)
class AppShellSchema:
    app_name: str
    app_subtitle: str
    pages: Tuple[PageDefinition, ...]
    topbar_actions: Tuple[ActionDefinition, ...]


APP_SHELL = AppShellSchema(
    app_name="ChatBridge",
    app_subtitle="用于 Linux 或无桌面环境的控制台入口。页面只负责管理与观察，Hub / Bridge 仍然使用原有本地 IPC。",
    pages=PRIMARY_PAGES,
    topbar_actions=TOPBAR_ACTIONS,
)
