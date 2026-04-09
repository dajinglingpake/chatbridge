from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from bridge_config import BridgeConfig
from core.accounts import build_account_options
from core.actions import RepairCommand, build_repair_command_models
from core.app_state import build_badge, build_issues, build_overview_lines, build_quickstart_lines, build_summary_text, decide_primary_action
from core.dashboard import DashboardState, load_dashboard_state
from core.sessions import SessionRow, build_session_detail, build_session_rows


Translator = Callable[..., str]


@dataclass
class HomeViewModel:
    badge_text: str
    badge_style: str
    overview_text: str
    summary_text: str
    primary_action: str
    primary_label: str
    primary_hint: str
    quickstart_text: str
    quickstart_status: str


@dataclass
class IssueViewModel:
    title: str
    detail: str


@dataclass
class CheckViewModel:
    label: str
    detail: str
    status_text: str
    ok: bool


@dataclass
class TaskViewModel:
    task_id: str
    created_at: str
    agent_name: str
    backend: str
    status: str
    session_name: str
    prompt_summary: str
    result_summary: str


@dataclass
class AccountOptionViewModel:
    account_id: str
    label: str
    selected: bool


@dataclass
class AgentOptionViewModel:
    agent_id: str
    label: str


@dataclass
class AgentManagementViewModel:
    agent_id: str
    name: str
    workdir: str
    session_file: str
    backend: str
    model: str
    prompt_prefix: str
    enabled: bool
    runtime_status: str
    queue_size: int


@dataclass
class ExternalAgentProcessViewModel:
    pid: int
    name: str
    backend: str
    session_hint: str
    command_line: str
    managed_label: str


@dataclass
class WeixinConversationBindingViewModel:
    sender_id: str
    agent_id: str
    current_session: str
    current_backend: str
    session_count: int
    updated_at: str
    latest_task_id: str
    latest_task_status: str
    latest_task_session: str


@dataclass
class WebConsoleViewModel:
    home: HomeViewModel
    log_dir: str
    active_account_id: str
    bridge_agent_id: str
    service_notice_enabled: bool
    config_notice_enabled: bool
    task_notice_enabled: bool
    issues: list[IssueViewModel]
    repair_commands: list[RepairCommand]
    checks: list[CheckViewModel]
    log_sections: list[tuple[str, str]]
    tasks: list[TaskViewModel]
    session_rows: list[SessionRow]
    selected_session_name: str
    selected_task_id: str
    selected_task_status: str
    selected_task_agent: str
    selected_task_backend: str
    task_status_options: list[str]
    task_agent_options: list[str]
    task_backend_options: list[str]
    task_total_count: int
    task_filtered_count: int
    session_detail_lines: list[str]
    session_conversation_lines: list[str]
    task_detail_lines: list[str]
    task_result_lines: list[str]
    agent_management: list[AgentManagementViewModel]
    external_agent_processes: list[ExternalAgentProcessViewModel]
    weixin_conversations: list[WeixinConversationBindingViewModel]
    account_options: list[AccountOptionViewModel]
    agent_options: list[AgentOptionViewModel]


@dataclass
class SessionDetailViewModel:
    rows: list[SessionRow]
    detail_text: str
    conversation_text: str


@dataclass
class IssuePanelViewModel:
    summary_text: str
    detail_text: str
    show_repair_button: bool
    show_manage_accounts_button: bool
    show_login_button: bool
    show_cleanup_button: bool
    show_open_dir_button: bool


@dataclass
class AccountManagementViewModel:
    active_account_id: str
    options: list[AccountOptionViewModel]


@dataclass
class DiagnosticsViewModel:
    label_text: str
    detail_text: str
    checks: list[CheckViewModel]


def summarize_text(value: str, limit: int = 96) -> str:
    compact = " ".join(value.split())
    if not compact:
        return "(empty)"
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 3]}..."


def build_home_view_model(
    snapshot: Any,
    checks: dict[str, Any],
    bridge_state: dict[str, Any],
    active_account_id: str,
    accounts_dir: Path,
    t: Translator,
) -> HomeViewModel:
    badge = build_badge(snapshot, t)
    overview_lines = build_overview_lines(snapshot, bridge_state, active_account_id, t)
    primary_action, primary_label, primary_hint = decide_primary_action(snapshot, checks, t)
    quickstart_lines, quickstart_status = build_quickstart_lines(snapshot, checks, accounts_dir, t)
    return HomeViewModel(
        badge_text=badge.text,
        badge_style=badge.style,
        overview_text="\n".join(overview_lines),
        summary_text=build_summary_text(snapshot, checks, t),
        primary_action=primary_action,
        primary_label=primary_label,
        primary_hint=primary_hint,
        quickstart_text="\n".join(quickstart_lines),
        quickstart_status=quickstart_status,
    )


def build_session_detail_view_model(
    hub_state: dict[str, Any],
    session_dir: Path,
    session_name: str,
    task_status_text: Callable[[str], str] | None = None,
    t: Translator | None = None,
) -> SessionDetailViewModel:
    detail = build_session_detail(hub_state, session_dir, session_name, task_status_text, t)
    return SessionDetailViewModel(
        rows=detail.rows,
        detail_text="\n".join(detail.detail_lines).strip(),
        conversation_text="\n".join(detail.conversation_lines).strip(),
    )


def build_issue_panel_view_model(
    snapshot: Any,
    bridge_state: dict[str, Any],
    checks: dict[str, Any],
    t: Translator,
) -> IssuePanelViewModel:
    issues = build_issues(snapshot, bridge_state, checks, t)
    if not issues:
        summary_text = t("ui.issue.none.summary")
        detail_text = t("ui.issue.none.detail")
    else:
        summary_text = t("ui.issue.summary.count", count=len(issues))
        detail_text = "\n\n".join(f"[{issue.title}]\n{issue.detail}" for issue in issues)

    issue_kinds = {issue.kind for issue in issues}
    return IssuePanelViewModel(
        summary_text=summary_text,
        detail_text=detail_text,
        show_repair_button="dependencies" in issue_kinds,
        show_manage_accounts_button=True,
        show_login_button="login" in issue_kinds,
        show_cleanup_button="processes" in issue_kinds,
        show_open_dir_button=True,
    )


def build_account_management_view_model(t: Translator) -> AccountManagementViewModel:
    config = BridgeConfig.load()
    built_options, active_index = build_account_options(config, t)
    options: list[AccountOptionViewModel] = []
    for index, item in enumerate(built_options):
        if item.key != "existing" or item.account is None:
            continue
        label = f"{item.account.account_id} {'(active)' if item.account.account_id == config.active_account_id else ''}"
        options.append(
            AccountOptionViewModel(
                account_id=item.account.account_id,
                label=label,
                selected=index == active_index,
            )
        )
    return AccountManagementViewModel(active_account_id=config.active_account_id, options=options)


def build_diagnostics_view_model(checks: dict[str, Any], diag_at: str, t: Translator) -> DiagnosticsViewModel:
    ordered_keys = ["python", "winget", "nvm", "psutil", "node", "npm", "codex", "claude", "opencode", "weixin_account", "project_files"]
    check_models: list[CheckViewModel] = []
    lines: list[str] = []
    for key in ordered_keys:
        item = checks.get(key)
        if item is None:
            continue
        status_text = t("ui.diagnostics.ok") if item.ok else t("ui.diagnostics.missing")
        check_model = CheckViewModel(
            label=str(item.label),
            detail=str(item.detail),
            status_text=status_text,
            ok=bool(item.ok),
        )
        check_models.append(check_model)
        lines.append(f"[{status_text}] {check_model.label}: {check_model.detail}")
    return DiagnosticsViewModel(
        label_text=t("ui.diagnostics.label", time=diag_at),
        detail_text="\n".join(lines),
        checks=check_models,
    )


def build_web_console_view_model(
    app_dir: Path,
    t: Translator,
    selected_session_name: str = "",
    selected_task_id: str = "",
    selected_task_status: str = "",
    selected_task_agent: str = "",
    selected_task_backend: str = "",
) -> WebConsoleViewModel:
    dashboard = load_dashboard_state(app_dir)
    return build_web_console_view_model_from_dashboard(
        dashboard,
        app_dir,
        t,
        selected_session_name=selected_session_name,
        selected_task_id=selected_task_id,
        selected_task_status=selected_task_status,
        selected_task_agent=selected_task_agent,
        selected_task_backend=selected_task_backend,
    )


def build_web_console_view_model_from_dashboard(
    dashboard: DashboardState,
    app_dir: Path,
    t: Translator,
    selected_session_name: str = "",
    selected_task_id: str = "",
    selected_task_status: str = "",
    selected_task_agent: str = "",
    selected_task_backend: str = "",
) -> WebConsoleViewModel:
    checks_map = dashboard.checks
    hub_state = dashboard.hub_state
    bridge_state = dashboard.bridge_state
    bridge_conversations = dashboard.bridge_conversations
    session_dir = app_dir / "sessions"
    session_rows = build_session_rows(hub_state, session_dir)
    available_session_names = {row.name for row in session_rows}
    resolved_session_name = selected_session_name if selected_session_name in available_session_names else ""
    if not resolved_session_name and session_rows:
        resolved_session_name = session_rows[0].name
    session_detail = build_session_detail_view_model(hub_state, session_dir, resolved_session_name)
    account_management = build_account_management_view_model(t)
    bridge_config = BridgeConfig.load()

    agent_options: list[AgentOptionViewModel] = []
    agent_management: list[AgentManagementViewModel] = []
    for agent in hub_state.get("agents") or []:
        agent_id = str(agent.get("id") or "")
        name = str(agent.get("name") or agent_id)
        runtime = agent.get("runtime") or {}
        if agent_id:
            agent_options.append(AgentOptionViewModel(agent_id=agent_id, label=f"{name} ({agent_id})"))
            agent_management.append(
                AgentManagementViewModel(
                    agent_id=agent_id,
                    name=name,
                    workdir=str(agent.get("workdir") or ""),
                    session_file=str(agent.get("session_file") or ""),
                    backend=str(agent.get("backend") or ""),
                    model=str(agent.get("model") or ""),
                    prompt_prefix=str(agent.get("prompt_prefix") or ""),
                    enabled=bool(agent.get("enabled", True)),
                    runtime_status=str(runtime.get("status") or "idle"),
                    queue_size=int(runtime.get("queue_size") or 0),
                )
            )
    if not agent_options:
        agent_options.append(AgentOptionViewModel(agent_id="main", label="默认会话 (main)"))

    external_agent_processes: list[ExternalAgentProcessViewModel] = []
    for process in dashboard.external_agent_processes or hub_state.get("external_agent_processes") or []:
        pid = int(process.get("pid") or 0)
        if pid <= 0:
            continue
        name = str(process.get("name") or "-")
        backend = str(process.get("backend") or "").strip().lower() or "unknown"
        command_line = str(process.get("command_line") or "").strip() or name
        external_agent_processes.append(
            ExternalAgentProcessViewModel(
                pid=pid,
                name=name,
                backend=backend,
                session_hint=str(process.get("session_hint") or ""),
                command_line=command_line,
                managed_label="外部 / 未接管",
            )
        )
    external_agent_processes.sort(key=lambda item: (item.backend, item.session_hint or "~", item.pid))

    weixin_conversations: list[WeixinConversationBindingViewModel] = []
    for sender_id, binding in sorted((bridge_conversations or {}).items()):
        if not isinstance(binding, dict):
            continue
        current_session = str(binding.get("current_session") or "default")
        sessions = binding.get("sessions") or {}
        if not isinstance(sessions, dict):
            sessions = {}
        current_meta = sessions.get(current_session) or {}
        latest_task = next(
            (
                task
                for task in (hub_state.get("tasks") or [])
                if str(task.get("sender_id") or "").strip() == str(sender_id)
            ),
            {},
        )
        weixin_conversations.append(
            WeixinConversationBindingViewModel(
                sender_id=str(sender_id),
                agent_id=str(bridge_config.backend_id or "main"),
                current_session=current_session,
                current_backend=str(current_meta.get("backend") or bridge_config.default_backend),
                session_count=len(sessions),
                updated_at=str(current_meta.get("updated_at") or current_meta.get("created_at") or "-"),
                latest_task_id=str(latest_task.get("id") or ""),
                latest_task_status=str(latest_task.get("status") or ""),
                latest_task_session=str(latest_task.get("session_name") or current_session),
            )
        )

    raw_tasks = list(hub_state.get("tasks") or [])
    task_status_options = sorted({str(task.get("status") or "") for task in raw_tasks if str(task.get("status") or "")})
    task_agent_options = sorted({str(task.get("agent_name") or task.get("agent_id") or "") for task in raw_tasks if str(task.get("agent_name") or task.get("agent_id") or "")})
    task_backend_options = sorted({str(task.get("backend") or "") for task in raw_tasks if str(task.get("backend") or "")})

    resolved_task_status = selected_task_status if selected_task_status in task_status_options else ""
    resolved_task_agent = selected_task_agent if selected_task_agent in task_agent_options else ""
    resolved_task_backend = selected_task_backend if selected_task_backend in task_backend_options else ""

    total_task_count = len(raw_tasks)
    filtered_raw_tasks = [
        task
        for task in raw_tasks
        if (not resolved_session_name or str(task.get("session_name") or "default") == resolved_session_name)
        and (not resolved_task_status or str(task.get("status") or "") == resolved_task_status)
        and (not resolved_task_agent or str(task.get("agent_name") or task.get("agent_id") or "") == resolved_task_agent)
        and (not resolved_task_backend or str(task.get("backend") or "") == resolved_task_backend)
    ]
    filtered_task_count = len(filtered_raw_tasks)
    if not filtered_raw_tasks:
        filtered_raw_tasks = raw_tasks

    tasks: list[TaskViewModel] = []
    for task in filtered_raw_tasks:
        tasks.append(
            TaskViewModel(
                task_id=str(task.get("id") or ""),
                created_at=str(task.get("created_at") or ""),
                agent_name=str(task.get("agent_name") or task.get("agent_id") or ""),
                backend=str(task.get("backend") or ""),
                status=str(task.get("status") or ""),
                session_name=str(task.get("session_name") or "default"),
                prompt_summary=summarize_text(str(task.get("prompt") or "")),
                result_summary=summarize_text(str(task.get("output") or task.get("error") or "")),
            )
        )
    available_task_ids = {task.task_id for task in tasks if task.task_id}
    resolved_task_id = selected_task_id if selected_task_id in available_task_ids else ""
    if not resolved_task_id and tasks:
        resolved_task_id = tasks[0].task_id
    selected_task = next((task for task in filtered_raw_tasks if str(task.get("id") or "") == resolved_task_id), None)
    if selected_task is None:
        task_detail_lines = ["先在上方选中一个任务。"]
        task_result_lines = ["这里会显示该任务的完整输出或错误。"]
    else:
        task_detail_lines = [
            f"任务 ID: {selected_task.get('id') or ''}",
            f"创建时间: {selected_task.get('created_at') or ''}",
            f"完成时间: {selected_task.get('finished_at') or '-'}",
            f"Agent: {selected_task.get('agent_name') or selected_task.get('agent_id') or ''}",
            f"后端: {selected_task.get('backend') or ''}",
            f"状态: {selected_task.get('status') or ''}",
            f"会话: {selected_task.get('session_name') or 'default'}",
            f"来源: {selected_task.get('source') or '-'}",
            "",
            "输入:",
            str(selected_task.get("prompt") or "(empty)"),
        ]
        task_result = str(selected_task.get("output") or selected_task.get("error") or "(empty)")
        task_result_lines = [task_result]

    checks: list[CheckViewModel] = []
    for check in checks_map.values():
        checks.append(
            CheckViewModel(
                label=str(check.label),
                detail=str(check.detail),
                status_text="OK" if check.ok else "MISSING",
                ok=bool(check.ok),
            )
        )

    issues = [
        IssueViewModel(title=item.title, detail=item.detail)
        for item in build_issues(dashboard.snapshot, bridge_state, checks_map, t)
    ]
    repair_commands = build_repair_command_models(checks_map, t)
    log_sections = [
        ("Hub stdout", dashboard.logs.get("hub_out", "(empty)")),
        ("Hub stderr", dashboard.logs.get("hub_err", "(empty)")),
        ("Bridge stdout", dashboard.logs.get("bridge_out", "(empty)")),
        ("Bridge stderr", dashboard.logs.get("bridge_err", "(empty)")),
    ]

    return WebConsoleViewModel(
        home=build_home_view_model(
            snapshot=dashboard.snapshot,
            checks=checks_map,
            bridge_state=bridge_state,
            active_account_id=dashboard.active_account_id,
            accounts_dir=app_dir / "accounts",
            t=t,
        ),
        log_dir=dashboard.snapshot.log_dir,
        active_account_id=account_management.active_account_id,
        bridge_agent_id=bridge_config.backend_id,
        service_notice_enabled=bridge_config.service_notice_enabled,
        config_notice_enabled=bridge_config.config_notice_enabled,
        task_notice_enabled=bridge_config.task_notice_enabled,
        issues=issues,
        repair_commands=repair_commands,
        checks=checks,
        log_sections=log_sections,
        tasks=tasks,
        session_rows=session_rows,
        selected_session_name=resolved_session_name,
        selected_task_id=resolved_task_id,
        selected_task_status=resolved_task_status,
        selected_task_agent=resolved_task_agent,
        selected_task_backend=resolved_task_backend,
        task_status_options=task_status_options,
        task_agent_options=task_agent_options,
        task_backend_options=task_backend_options,
        task_total_count=total_task_count,
        task_filtered_count=filtered_task_count,
        session_detail_lines=session_detail.detail_text.splitlines(),
        session_conversation_lines=session_detail.conversation_text.splitlines(),
        task_detail_lines=task_detail_lines,
        task_result_lines=task_result_lines,
        agent_management=agent_management,
        external_agent_processes=external_agent_processes,
        weixin_conversations=weixin_conversations,
        account_options=account_management.options,
        agent_options=agent_options,
    )
