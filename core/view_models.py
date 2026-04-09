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
    created_at: str
    agent_name: str
    backend: str
    status: str
    prompt: str
    result_text: str


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
class WebConsoleViewModel:
    home: HomeViewModel
    log_dir: str
    active_account_id: str
    issues: list[IssueViewModel]
    repair_commands: list[RepairCommand]
    checks: list[CheckViewModel]
    tasks: list[TaskViewModel]
    session_rows: list[SessionRow]
    session_detail_lines: list[str]
    session_conversation_lines: list[str]
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
    ordered_keys = ["python", "winget", "nvm", "pyside6", "psutil", "node", "npm", "codex", "opencode", "weixin_account", "project_files"]
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


def build_web_console_view_model(app_dir: Path, t: Translator) -> WebConsoleViewModel:
    dashboard = load_dashboard_state(app_dir)
    return build_web_console_view_model_from_dashboard(dashboard, app_dir, t)


def build_web_console_view_model_from_dashboard(
    dashboard: DashboardState,
    app_dir: Path,
    t: Translator,
) -> WebConsoleViewModel:
    checks_map = dashboard.checks
    hub_state = dashboard.hub_state
    bridge_state = dashboard.bridge_state
    session_dir = app_dir / ".runtime" / "sessions"
    session_rows = build_session_rows(hub_state, session_dir)
    default_session_name = session_rows[0].name if session_rows else ""
    session_detail = build_session_detail_view_model(hub_state, session_dir, default_session_name)
    account_management = build_account_management_view_model(t)

    agent_options: list[AgentOptionViewModel] = []
    for agent in hub_state.get("agents") or []:
        agent_id = str(agent.get("id") or "")
        name = str(agent.get("name") or agent_id)
        if agent_id:
            agent_options.append(AgentOptionViewModel(agent_id=agent_id, label=f"{name} ({agent_id})"))
    if not agent_options:
        agent_options.append(AgentOptionViewModel(agent_id="main", label="默认会话 (main)"))

    tasks: list[TaskViewModel] = []
    for task in (hub_state.get("tasks") or [])[:12]:
        tasks.append(
            TaskViewModel(
                created_at=str(task.get("created_at") or ""),
                agent_name=str(task.get("agent_name") or task.get("agent_id") or ""),
                backend=str(task.get("backend") or ""),
                status=str(task.get("status") or ""),
                prompt=str(task.get("prompt") or ""),
                result_text=str(task.get("output") or task.get("error") or ""),
            )
        )

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
        issues=issues,
        repair_commands=repair_commands,
        checks=checks,
        tasks=tasks,
        session_rows=session_rows,
        session_detail_lines=session_detail.detail_text.splitlines(),
        session_conversation_lines=session_detail.conversation_text.splitlines(),
        account_options=account_management.options,
        agent_options=agent_options,
    )
