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

MOBILE_PAGE = PageDefinition(
    key="mobile",
    title="手机入口",
    anchor="mobile",
    description="扫码打开手机端 UI。",
)

STREAM_PAGE = PageDefinition(
    key="stream",
    title="实时对话",
    anchor="stream",
    description="查看最近任务状态、进度和输出。",
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
    MOBILE_PAGE,
    STREAM_PAGE,
    DIAGNOSTICS_PAGE,
)
