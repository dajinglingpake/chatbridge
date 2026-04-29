from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PageDefinition:
    key: str
    title: str
    anchor: str
    description: str


HOME_PAGE = PageDefinition(
    key="home",
    title="首页",
    anchor="home",
    description="服务状态、任务提交、账号切换和通知设置。",
)

SESSIONS_PAGE = PageDefinition(
    key="sessions",
    title="会话",
    anchor="sessions",
    description="会话列表、最近任务和默认会话预览。",
)

DIAGNOSTICS_PAGE = PageDefinition(
    key="diagnostics",
    title="诊断与日志",
    anchor="diagnostics",
    description="环境检查结果和运行状态观察入口。",
)

PRIMARY_PAGES: tuple[PageDefinition, ...] = (
    HOME_PAGE,
    SESSIONS_PAGE,
    DIAGNOSTICS_PAGE,
)
