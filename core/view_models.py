from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Callable, TypeVar

from bridge_config import BridgeConfig
from core.accounts import build_account_options
from core.actions import RepairCommand, build_repair_command_models
from core.app_state import build_badge, build_summary_text, decide_primary_action
from core.dashboard import DashboardState, load_dashboard_state
from core.sessions import SessionRow, build_session_detail, build_session_rows
from core.state_models import CheckSnapshot, HubStateSnapshot, HubTask, RuntimeSnapshot


Translator = Callable[..., str]
ItemT = TypeVar("ItemT")


def _t(t: Translator, key: str, fallback: str, **kwargs: object) -> str:
    value = t(key, **kwargs)
    return value if value != key else fallback.format(**kwargs)


def _task_status_label(t: Translator, status: str) -> str:
    cleaned = (status or "").strip().lower()
    if not cleaned:
        return _t(t, "bridge.task.status.unknown", "未知")
    value = t(f"bridge.task.status.{cleaned}")
    return value if value != f"bridge.task.status.{cleaned}" else status


def _short_account_id(account_id: str) -> str:
    cleaned = str(account_id or "").strip()
    if len(cleaned) <= 12:
        return cleaned
    return f"{cleaned[:6]}...{cleaned[-6:]}"


def _account_display_label(t: Translator, account_id: str) -> str:
    cleaned = str(account_id or "").strip()
    if not cleaned:
        return _t(t, "ui.web.value.unset", "未设置")
    if cleaned.endswith("@im.bot"):
        return _t(t, "ui.account.display.bot", "微信 Bot 账号 ({account})", account=_short_account_id(cleaned))
    return cleaned


def _checks_progress_label(t: Translator, progress_text: str) -> str:
    text = (progress_text or "").strip()
    if not text:
        return ""
    if text == "环境检查已完成":
        return _t(t, "ui.diagnostics.progress.done", "环境检查已完成")
    prefix = "环境检查进行中："
    marker = "，当前步骤："
    if not text.startswith(prefix) or marker not in text:
        return text
    ratio, step_label = text[len(prefix) :].split(marker, 1)
    if "/" not in ratio:
        return text
    current, total = ratio.split("/", 1)
    step_key = {
        "Python": "python",
        "Node 环境": "node_runtime",
        "Agent CLI": "agent_clis",
        "Python 依赖": "psutil",
        "微信账号文件": "weixin_account",
        "项目文件": "project_files",
        "已完成": "done",
    }.get(step_label.strip(), "")
    translated_step = _t(t, f"ui.diagnostics.step.{step_key}", step_label.strip()) if step_key else step_label.strip()
    return _t(t, "ui.diagnostics.progress.running", "环境检查进行中：{current}/{total}，当前步骤：{step}", current=current, total=total, step=translated_step)


@dataclass
class HomeViewModel:
    badge_text: str
    badge_style: str
    summary_text: str
    primary_hint: str


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
class AgentEntryViewModel:
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
    active_account_label: str
    bridge_agent_id: str
    service_notice_enabled: bool
    config_notice_enabled: bool
    task_notice_enabled: bool
    repair_commands: list[RepairCommand]
    checks: list[CheckViewModel]
    checks_in_progress: bool
    checks_progress_text: str
    log_sections: list[tuple[str, str]]
    tasks: list[TaskViewModel]
    session_rows: list[SessionRow]
    session_page: int
    session_total_count: int
    session_total_pages: int
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
    task_page: int
    task_total_pages: int
    session_detail_lines: list[str]
    session_conversation_lines: list[str]
    task_detail_lines: list[str]
    task_result_lines: list[str]
    agent_entries: list[AgentEntryViewModel]
    agent_page: int
    agent_total_count: int
    agent_total_pages: int
    external_agent_processes: list[ExternalAgentProcessViewModel]
    weixin_conversations: list[WeixinConversationBindingViewModel]
    account_options: list[AccountOptionViewModel]
    agent_options: list[AgentOptionViewModel]
    checks_page: int
    checks_total_count: int
    checks_total_pages: int


@dataclass
class SessionDetailViewModel:
    rows: list[SessionRow]
    detail_text: str
    conversation_text: str


@dataclass
class AccountSelectionViewModel:
    active_account_id: str
    active_account_label: str
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


def paginate_items(items: list[ItemT], page: int, page_size: int) -> tuple[list[ItemT], int, int]:
    total_count = len(items)
    normalized_page_size = max(1, int(page_size))
    total_pages = max(1, math.ceil(total_count / normalized_page_size)) if total_count else 1
    normalized_page = min(max(1, int(page)), total_pages)
    start = (normalized_page - 1) * normalized_page_size
    end = start + normalized_page_size
    return items[start:end], normalized_page, total_pages


def build_home_view_model(
    snapshot: RuntimeSnapshot,
    checks: dict[str, CheckSnapshot],
    t: Translator,
) -> HomeViewModel:
    badge = build_badge(snapshot, t)
    _, _, primary_hint = decide_primary_action(snapshot, checks, t)
    return HomeViewModel(
        badge_text=badge.text,
        badge_style=badge.style,
        summary_text=build_summary_text(snapshot, checks, t),
        primary_hint=primary_hint,
    )


def build_session_detail_view_model(
    hub_state: HubStateSnapshot,
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


def build_account_selection_view_model(t: Translator, config: BridgeConfig | None = None) -> AccountSelectionViewModel:
    config = config or BridgeConfig.load()
    built_options, active_index = build_account_options(config, t)
    options: list[AccountOptionViewModel] = []
    for index, item in enumerate(built_options):
        if item.key != "existing" or item.account is None:
            continue
        if not item.account.is_usable:
            continue
        active_marker = _t(t, "ui.account.option.active", "[当前]") if item.account.account_id == config.active_account_id else ""
        label = f"{active_marker} {_account_display_label(t, item.account.account_id)}".strip()
        options.append(
            AccountOptionViewModel(
                account_id=item.account.account_id,
                label=label,
                selected=index == active_index,
            )
        )
    return AccountSelectionViewModel(
        active_account_id=config.active_account_id,
        active_account_label=_account_display_label(t, config.active_account_id),
        options=options,
    )


def build_diagnostics_view_model(checks: dict[str, CheckSnapshot], diag_at: str, t: Translator) -> DiagnosticsViewModel:
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
    page_key: str = "home",
    session_page: int = 1,
    task_page: int = 1,
    agent_page: int = 1,
    checks_page: int = 1,
    load_session_detail: bool = False,
    load_task_detail: bool = False,
    selected_session_name: str = "",
    selected_task_id: str = "",
    selected_task_status: str = "",
    selected_task_agent: str = "",
    selected_task_backend: str = "",
) -> WebConsoleViewModel:
    dashboard = load_dashboard_state(app_dir, page_key=page_key)
    return build_web_console_view_model_from_dashboard(
        dashboard,
        app_dir,
        t,
        page_key=page_key,
        session_page=session_page,
        task_page=task_page,
        agent_page=agent_page,
        checks_page=checks_page,
        load_session_detail=load_session_detail,
        load_task_detail=load_task_detail,
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
    page_key: str = "home",
    session_page: int = 1,
    task_page: int = 1,
    agent_page: int = 1,
    checks_page: int = 1,
    load_session_detail: bool = False,
    load_task_detail: bool = False,
    selected_session_name: str = "",
    selected_task_id: str = "",
    selected_task_status: str = "",
    selected_task_agent: str = "",
    selected_task_backend: str = "",
) -> WebConsoleViewModel:
    normalized_page_key = (page_key or "home").strip().lower()
    checks_map = dashboard.checks
    hub_state = dashboard.hub_state
    bridge_conversations = dashboard.bridge_conversations
    session_dir = app_dir / "sessions"
    session_rows: list[SessionRow] = []
    resolved_session_name = ""
    session_detail = SessionDetailViewModel(rows=[], detail_text="", conversation_text="")
    session_total_count = 0
    session_total_pages = 1
    if normalized_page_key == "sessions":
        all_session_rows = build_session_rows(hub_state, session_dir)
        available_session_names = {row.name for row in all_session_rows}
        resolved_session_name = selected_session_name if selected_session_name in available_session_names else ""
        if not resolved_session_name and all_session_rows:
            resolved_session_name = all_session_rows[0].name
        if load_session_detail and resolved_session_name:
            session_detail = build_session_detail_view_model(hub_state, session_dir, resolved_session_name, t=t)
        else:
            session_detail = SessionDetailViewModel(
                rows=[],
                detail_text=_t(t, "ui.web.sessions.lazy_detail", "点击“加载会话详情”后再读取会话文件和最近对话。"),
                conversation_text=_t(t, "ui.web.sessions.lazy_preview", "当前为了避免切页卡顿，默认不自动加载会话详情和会话预览。"),
            )
        session_total_count = len(all_session_rows)
        session_rows, session_page, session_total_pages = paginate_items(all_session_rows, session_page, 10)
    bridge_config = dashboard.bridge_config
    account_selection = build_account_selection_view_model(t, bridge_config)

    agent_options: list[AgentOptionViewModel] = []
    agent_entries: list[AgentEntryViewModel] = []
    for agent in hub_state.agents:
        agent_id = agent.id
        name = agent.name or agent_id
        runtime = agent.runtime
        if agent_id:
            agent_options.append(AgentOptionViewModel(agent_id=agent_id, label=f"{name} ({agent_id})"))
            agent_entries.append(
                AgentEntryViewModel(
                    agent_id=agent_id,
                    name=name,
                    workdir=agent.workdir,
                    session_file=agent.session_file,
                    backend=agent.backend,
                    model=agent.model,
                    prompt_prefix=agent.prompt_prefix,
                    enabled=agent.enabled,
                    runtime_status=runtime.status or "idle",
                    queue_size=runtime.queue_size,
                )
            )
    if not agent_options:
        agent_options.append(AgentOptionViewModel(agent_id="main", label=_t(t, "ui.web.agents.default_option", "默认会话 (main)")))
    all_agent_entries = agent_entries
    agent_total_count = len(all_agent_entries)
    agent_entries, agent_page, agent_total_pages = paginate_items(all_agent_entries, agent_page, 10)

    external_agent_processes: list[ExternalAgentProcessViewModel] = []
    for process in dashboard.external_agent_processes or hub_state.external_agent_processes:
        pid = process.pid
        if pid <= 0:
            continue
        name = process.name or "-"
        backend = process.backend or "unknown"
        command_line = process.command_line.strip() or name
        external_agent_processes.append(
            ExternalAgentProcessViewModel(
                pid=pid,
                name=name,
                backend=backend,
                session_hint=process.session_hint,
                command_line=command_line,
                managed_label=_t(t, "ui.web.external.unmanaged", "外部 / 未接管"),
            )
        )
    external_agent_processes.sort(key=lambda item: (item.backend, item.session_hint or "~", item.pid))

    weixin_conversations: list[WeixinConversationBindingViewModel] = []
    if normalized_page_key == "sessions":
        latest_task_by_sender: dict[str, HubTask] = {}
        for task in hub_state.tasks:
            sender_id = task.sender_id.strip()
            if sender_id and sender_id not in latest_task_by_sender:
                latest_task_by_sender[sender_id] = task
        for sender_id, binding in sorted((bridge_conversations or {}).items()):
            current_session = binding.current_session
            sessions = binding.sessions
            current_meta = sessions.get(current_session)
            latest_task = latest_task_by_sender.get(str(sender_id))
            current_backend = (current_meta.backend if current_meta is not None else bridge_config.default_backend)
            updated_at = "-"
            if current_meta is not None:
                updated_at = current_meta.updated_at or current_meta.created_at or "-"
            weixin_conversations.append(
                WeixinConversationBindingViewModel(
                    sender_id=str(sender_id),
                    agent_id=str(bridge_config.backend_id or "main"),
                    current_session=current_session,
                    current_backend=current_backend,
                    session_count=len(sessions),
                    updated_at=updated_at,
                    latest_task_id=latest_task.id if latest_task is not None else "",
                    latest_task_status=_task_status_label(t, latest_task.status) if latest_task is not None else "",
                    latest_task_session=(latest_task.session_name or current_session) if latest_task is not None else current_session,
                )
            )

    raw_tasks = list(hub_state.tasks)
    task_status_options: list[str] = []
    task_agent_options: list[str] = []
    task_backend_options: list[str] = []
    resolved_task_status = ""
    resolved_task_agent = ""
    resolved_task_backend = ""
    total_task_count = 0
    filtered_task_count = 0
    tasks: list[TaskViewModel] = []
    resolved_task_id = ""
    task_total_pages = 1
    task_detail_lines = [_t(t, "ui.web.tasks.switch_to_sessions", "先切换到会话模块查看任务详情。")] if normalized_page_key != "sessions" else [_t(t, "ui.web.tasks.select_task_first", "先在上方选中一个任务。")]
    task_result_lines = [_t(t, "ui.web.tasks.output_placeholder", "这里会显示该任务的完整输出或错误。")]
    if normalized_page_key == "sessions":
        task_status_options = sorted({task.status for task in raw_tasks if task.status})
        task_agent_options = sorted({(task.agent_name or task.agent_id) for task in raw_tasks if (task.agent_name or task.agent_id)})
        task_backend_options = sorted({task.backend for task in raw_tasks if task.backend})

        resolved_task_status = selected_task_status if selected_task_status in task_status_options else ""
        resolved_task_agent = selected_task_agent if selected_task_agent in task_agent_options else ""
        resolved_task_backend = selected_task_backend if selected_task_backend in task_backend_options else ""

        total_task_count = len(raw_tasks)
        filtered_raw_tasks = [
            task
            for task in raw_tasks
            if (not resolved_session_name or (task.session_name or "default") == resolved_session_name)
            and (not resolved_task_status or task.status == resolved_task_status)
            and (not resolved_task_agent or (task.agent_name or task.agent_id) == resolved_task_agent)
            and (not resolved_task_backend or task.backend == resolved_task_backend)
        ]
        filtered_task_count = len(filtered_raw_tasks)
        if not filtered_raw_tasks:
            filtered_raw_tasks = raw_tasks

        paged_raw_tasks, task_page, task_total_pages = paginate_items(filtered_raw_tasks, task_page, 8)
        for task in paged_raw_tasks:
            tasks.append(
                TaskViewModel(
                    task_id=task.id,
                    created_at=task.created_at,
                    agent_name=task.agent_name or task.agent_id,
                    backend=task.backend,
                    status=_task_status_label(t, task.status),
                    session_name=task.session_name or "default",
                    prompt_summary=summarize_text(task.prompt),
                    result_summary=summarize_text(task.output or task.error),
                )
            )
        available_task_ids = {task.task_id for task in tasks if task.task_id}
        resolved_task_id = selected_task_id if selected_task_id in available_task_ids else ""
        if not resolved_task_id and tasks:
            resolved_task_id = tasks[0].task_id
        selected_task = next((task for task in filtered_raw_tasks if task.id == resolved_task_id), None)
        if selected_task is not None and load_task_detail:
            task_detail_lines = [
                _t(t, "ui.web.task_detail.id", "任务 ID: {value}", value=selected_task.id),
                _t(t, "ui.web.task_detail.created_at", "创建时间: {value}", value=selected_task.created_at),
                _t(t, "ui.web.task_detail.finished_at", "完成时间: {value}", value=selected_task.finished_at or "-"),
                f"Agent: {selected_task.agent_name or selected_task.agent_id}",
                _t(t, "ui.web.task_detail.backend", "后端: {value}", value=selected_task.backend),
                _t(t, "ui.web.task_detail.status", "状态: {value}", value=_task_status_label(t, selected_task.status)),
                _t(t, "ui.web.task_detail.session", "会话: {value}", value=selected_task.session_name or "default"),
                _t(t, "ui.web.task_detail.source", "来源: {value}", value=selected_task.source or "-"),
                "",
                _t(t, "ui.web.task_detail.input", "输入:"),
                selected_task.prompt or "(empty)",
            ]
            task_result = selected_task.output or selected_task.error or "(empty)"
            task_result_lines = [task_result]
        elif selected_task is not None:
            task_detail_lines = [_t(t, "ui.web.tasks.lazy_detail", "点击“加载任务详情”后再读取完整输入和输出。")]
            task_result_lines = [_t(t, "ui.web.tasks.lazy_output", "当前为了避免卡顿，默认不自动展示完整输出。")]

    all_checks: list[CheckViewModel] = []
    for check in checks_map.values():
        all_checks.append(
            CheckViewModel(
                label=str(check.label),
                detail=str(check.detail),
                status_text=_t(t, "ui.diagnostics.ok", "OK") if check.ok else _t(t, "ui.diagnostics.missing", "缺失"),
                ok=bool(check.ok),
            )
        )
    checks_total_count = len(all_checks)
    checks, checks_page, checks_total_pages = paginate_items(all_checks, checks_page, 10)

    repair_commands = build_repair_command_models(checks_map, t) if normalized_page_key == "diagnostics" else []
    log_sections = [
        ("Hub stdout", dashboard.logs.get("hub_out", "(empty)")),
        ("Hub stderr", dashboard.logs.get("hub_err", "(empty)")),
        ("Bridge stdout", dashboard.logs.get("bridge_out", "(empty)")),
        ("Bridge stderr", dashboard.logs.get("bridge_err", "(empty)")),
    ] if normalized_page_key == "diagnostics" else []

    return WebConsoleViewModel(
        home=build_home_view_model(
            snapshot=dashboard.snapshot,
            checks=checks_map,
            t=t,
        ),
        log_dir=dashboard.snapshot.log_dir,
        active_account_id=account_selection.active_account_id,
        active_account_label=account_selection.active_account_label,
        bridge_agent_id=bridge_config.backend_id,
        service_notice_enabled=bridge_config.service_notice_enabled,
        config_notice_enabled=bridge_config.config_notice_enabled,
        task_notice_enabled=bridge_config.task_notice_enabled,
        repair_commands=repair_commands,
        checks=checks,
        checks_in_progress=dashboard.checks_in_progress,
        checks_progress_text=_checks_progress_label(t, dashboard.checks_progress_text),
        log_sections=log_sections,
        tasks=tasks,
        session_rows=session_rows,
        session_page=session_page,
        session_total_count=session_total_count,
        session_total_pages=session_total_pages,
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
        task_page=task_page,
        task_total_pages=task_total_pages,
        session_detail_lines=session_detail.detail_text.splitlines(),
        session_conversation_lines=session_detail.conversation_text.splitlines(),
        task_detail_lines=task_detail_lines,
        task_result_lines=task_result_lines,
        agent_entries=agent_entries,
        agent_page=agent_page,
        agent_total_count=agent_total_count,
        agent_total_pages=agent_total_pages,
        external_agent_processes=external_agent_processes,
        weixin_conversations=weixin_conversations,
        account_options=account_selection.options,
        agent_options=agent_options,
        checks_page=checks_page,
        checks_total_count=checks_total_count,
        checks_total_pages=checks_total_pages,
    )
