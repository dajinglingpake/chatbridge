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
    description="状态总览、主动作、任务提交和账号切换。",
)

ISSUES_PAGE = PageDefinition(
    key="issues",
    title="异常",
    anchor="issues",
    description="当前需要处理的问题和修复建议。",
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
    ISSUES_PAGE,
    SESSIONS_PAGE,
    DIAGNOSTICS_PAGE,
)
