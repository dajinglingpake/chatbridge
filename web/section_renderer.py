from __future__ import annotations

import html

from core.navigation import DIAGNOSTICS_PAGE, HOME_PAGE, ISSUES_PAGE, SESSIONS_PAGE
from core.view_models import WebConsoleViewModel


def _escape(value: object) -> str:
    return html.escape(str(value or ""))


def render_home_sections(model: WebConsoleViewModel) -> str:
    agent_options = "".join(
        f"<option value='{_escape(agent.agent_id)}'>{_escape(agent.label)}</option>"
        for agent in model.agent_options
    ) or "<option value='main'>默认会话 (main)</option>"
    account_options = "".join(
        f"<option value='{_escape(item.account_id)}'{' selected' if item.selected else ''}>{_escape(item.label)}</option>"
        for item in model.account_options
    ) or "<option value=''>暂无账号</option>"
    return (
        "<section id='home' class='page-section'>"
        "<div class='section-heading'>"
        f"<h2>{_escape(HOME_PAGE.title)}</h2>"
        f"<p>{_escape(HOME_PAGE.description)}</p>"
        "</div>"
        "<div class='grid'>"
        "<section class='panel'>"
        "<h3>运行状态</h3>"
        f"<p><strong>{_escape(model.home.badge_text)}</strong></p>"
        f"<pre>{_escape(model.home.overview_text)}</pre>"
        f"<p class='muted'>日志目录：{_escape(model.log_dir)}</p>"
        "<div class='actions'>"
        "<form method='post' action='/action/start'><button>启动服务</button></form>"
        "<form method='post' action='/action/stop'><button class='secondary'>停止服务</button></form>"
        "<form method='post' action='/action/restart'><button class='secondary'>重启服务</button></form>"
        "<form method='post' action='/action/emergency-stop'><button class='danger'>紧急停止</button></form>"
        "</div>"
        "</section>"
        "<section class='panel'>"
        "<h3>当前建议</h3>"
        f"<p><strong>{_escape(model.home.summary_text)}</strong></p>"
        f"<p>主动作：{_escape(model.home.primary_label)}（{_escape(model.home.primary_action)}）</p>"
        f"<p class='muted'>{_escape(model.home.primary_hint)}</p>"
        "<label>Quick Start</label>"
        f"<pre>{_escape(model.home.quickstart_text)}</pre>"
        f"<p class='muted'>{_escape(model.home.quickstart_status)}</p>"
        "</section>"
        "<section class='panel'>"
        "<h3>提交任务</h3>"
        "<form method='post' action='/action/submit-task'>"
        "<label for='agent_id'>Agent</label>"
        f"<select id='agent_id' name='agent_id'>{agent_options}</select>"
        "<label for='backend'>后端</label>"
        "<select id='backend' name='backend'>"
        "<option value=''>跟随 Agent 默认配置</option>"
        "<option value='codex'>codex</option>"
        "<option value='opencode'>opencode</option>"
        "</select>"
        "<label for='session_name'>会话名</label>"
        "<input id='session_name' name='session_name' placeholder='default'>"
        "<label for='prompt'>Prompt</label>"
        "<textarea id='prompt' name='prompt' placeholder='输入要发给 Agent 的内容'></textarea>"
        "<div class='actions'><button>提交到 Hub</button></div>"
        "</form>"
        "</section>"
        "<section class='panel'>"
        "<h3>账号管理</h3>"
        f"<p>当前激活账号：<strong>{_escape(model.active_account_id)}</strong></p>"
        "<form method='post' action='/action/switch-account'>"
        "<label for='account_id'>切换账号</label>"
        f"<select id='account_id' name='account_id'>{account_options}</select>"
        "<div class='actions'><button class='secondary'>切换当前账号</button></div>"
        "</form>"
        "</section>"
        "</div>"
        "</section>"
    )


def render_issue_section(model: WebConsoleViewModel) -> str:
    issue_html = "<p>当前没有需要手动处理的异常。</p>" if not model.issues else "".join(
        f"<div><strong>{_escape(item.title)}</strong><pre>{_escape(item.detail)}</pre></div>" for item in model.issues
    )
    repair_html = ""
    if model.repair_lines:
        repair_html = (
            "<section class='panel' style='margin-top:16px;'>"
            "<h2>修复建议</h2>"
            f"<pre>{_escape(chr(10).join(model.repair_lines))}</pre>"
            "</section>"
        )
    return (
        "<section id='issues' class='page-section'>"
        "<div class='section-heading'>"
        f"<h2>{_escape(ISSUES_PAGE.title)}</h2>"
        f"<p>{_escape(ISSUES_PAGE.description)}</p>"
        "</div>"
        "<section class='panel'>"
        "<h3>问题列表</h3>"
        f"{issue_html}"
        "</section>"
        f"{repair_html}"
        "</section>"
    )


def render_diagnostics_section(model: WebConsoleViewModel) -> str:
    check_rows = "".join(
        "<tr>"
        f"<td>{_escape(check.label)}</td>"
        f"<td class='{'ok' if check.ok else 'bad'}'>{_escape(check.status_text)}</td>"
        f"<td>{_escape(check.detail)}</td>"
        "</tr>"
        for check in model.checks
    )
    return (
        "<section id='diagnostics' class='page-section'>"
        "<div class='section-heading'>"
        f"<h2>{_escape(DIAGNOSTICS_PAGE.title)}</h2>"
        f"<p>{_escape(DIAGNOSTICS_PAGE.description)}</p>"
        "</div>"
        "<section class='panel'>"
        "<h3>环境检查</h3>"
        "<table>"
        "<thead><tr><th>项目</th><th>状态</th><th>详情</th></tr></thead>"
        f"<tbody>{check_rows}</tbody>"
        "</table>"
        "</section>"
        "</section>"
    )


def render_task_section(model: WebConsoleViewModel) -> str:
    task_rows = "".join(
        "<tr>"
        f"<td>{_escape(task.created_at)}</td>"
        f"<td>{_escape(task.agent_name)}</td>"
        f"<td>{_escape(task.backend)}</td>"
        f"<td>{_escape(task.status)}</td>"
        f"<td><pre>{_escape(task.prompt)}</pre></td>"
        f"<td><pre>{_escape(task.result_text)}</pre></td>"
        "</tr>"
        for task in model.tasks
    ) or "<tr><td colspan='6'>暂无任务记录</td></tr>"
    return (
        "<section class='panel' style='margin-top:16px;'>"
        "<h3>最近任务</h3>"
        "<table>"
        "<thead><tr><th>时间</th><th>Agent</th><th>后端</th><th>状态</th><th>输入</th><th>输出/错误</th></tr></thead>"
        f"<tbody>{task_rows}</tbody>"
        "</table>"
        "</section>"
    )


def render_session_section(model: WebConsoleViewModel) -> str:
    session_rows = "".join(
        "<tr>"
        f"<td>{_escape(item.name)}</td>"
        f"<td>{_escape(item.status)}</td>"
        f"<td>{item.queue_size}</td>"
        f"<td>{item.success_count}</td>"
        f"<td>{item.failure_count}</td>"
        "</tr>"
        for item in model.session_rows
    ) or "<tr><td colspan='5'>暂无会话</td></tr>"
    return (
        "<section id='sessions' class='page-section'>"
        "<div class='section-heading'>"
        f"<h2>{_escape(SESSIONS_PAGE.title)}</h2>"
        f"<p>{_escape(SESSIONS_PAGE.description)}</p>"
        "</div>"
        "<section class='panel'>"
        "<h3>会话概览</h3>"
        "<table>"
        "<thead><tr><th>会话</th><th>状态</th><th>队列</th><th>成功</th><th>失败</th></tr></thead>"
        f"<tbody>{session_rows}</tbody>"
        "</table>"
        "<label>默认会话详情</label>"
        f"<pre>{_escape(chr(10).join(model.session_detail_lines))}</pre>"
        "<label>默认会话预览</label>"
        f"<pre>{_escape(chr(10).join(model.session_conversation_lines))}</pre>"
        "</section>"
        f"{render_task_section(model)}"
        "</section>"
    )
