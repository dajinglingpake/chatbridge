from __future__ import annotations

from pathlib import Path

from core.action_defs import AUTO_REFRESH_OFF_ACTION, AUTO_REFRESH_ON_ACTION
from core.app_service import delete_agent, reset_weixin_conversation, run_named_action, run_repair_command, save_agent, set_weixin_notice_enabled, submit_hub_task, switch_active_account, switch_bridge_agent, switch_weixin_session_backend, terminate_external_agent
from core.navigation import PRIMARY_PAGES
from core.shell_schema import APP_SHELL
from core.view_models import build_web_console_view_model
from localization import Localizer
from ui.action_router import execute_primary_action, execute_topbar_action
from ui.qr_login import install_qr_login_dialog
from ui.sections import render_diagnostics_section, render_home_section, render_issues_section, render_sessions_section


APP_DIR = Path(__file__).resolve().parent.parent


def _load_nicegui():
    try:
        from nicegui import ui
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise SystemExit(
            "Missing dependency: nicegui. "
            "Linux 请优先运行 `./start-chatbridge-web.sh` 自动安装依赖，"
            "或手动执行 `python3 -m pip install -r requirements.txt`。"
        ) from exc
    return ui


def create_ui() -> None:
    ui = _load_nicegui()
    localizer = Localizer()
    ui.add_head_html(
        """
        <style>
        :root {
            --cb-bg: #f3efe7;
            --cb-surface: rgba(255, 252, 246, 0.88);
            --cb-surface-strong: #fffdf8;
            --cb-border: rgba(113, 86, 58, 0.16);
            --cb-ink: #201813;
            --cb-muted: #736150;
            --cb-accent: #b85c38;
            --cb-accent-deep: #8e4325;
            --cb-accent-soft: rgba(184, 92, 56, 0.12);
            --cb-ok: #2f6a4f;
            --cb-warn: #b06b1e;
            --cb-shadow: 0 22px 50px rgba(58, 39, 23, 0.10);
            --cb-radius-lg: 24px;
            --cb-radius-md: 18px;
        }
        body {
            background:
                radial-gradient(circle at top left, rgba(211, 152, 99, 0.28), transparent 28rem),
                radial-gradient(circle at top right, rgba(126, 158, 122, 0.18), transparent 26rem),
                linear-gradient(180deg, #f7f2ea 0%, var(--cb-bg) 100%);
            color: var(--cb-ink);
        }
        .nicegui-content {
            background: transparent !important;
        }
        .cb-shell-header {
            backdrop-filter: blur(18px);
            background: rgba(249, 244, 235, 0.88);
            border-bottom: 1px solid rgba(113, 86, 58, 0.14);
        }
        .cb-shell-nav {
            backdrop-filter: blur(16px);
            background: rgba(255, 250, 243, 0.84);
            border-bottom: 1px solid rgba(113, 86, 58, 0.10);
        }
        .cb-nav-button {
            min-width: 6.5rem;
        }
        .cb-nav-button.q-btn--outline {
            border-color: rgba(113, 86, 58, 0.18);
            color: var(--cb-muted);
            background: rgba(255, 255, 255, 0.45);
        }
        .cb-nav-button.cb-nav-active {
            background: linear-gradient(135deg, var(--cb-accent) 0%, #cf7c50 100%) !important;
            color: #fff8f2 !important;
            box-shadow: 0 10px 24px rgba(184, 92, 56, 0.22);
        }
        .cb-card, .cb-soft-card, .cb-code {
            border-radius: var(--cb-radius-lg);
            border: 1px solid var(--cb-border);
            box-shadow: var(--cb-shadow);
        }
        .cb-card {
            background: var(--cb-surface);
            backdrop-filter: blur(12px);
        }
        .cb-soft-card {
            background: linear-gradient(180deg, rgba(255,255,255,0.84), rgba(251,245,236,0.92));
        }
        .cb-hero {
            background:
                linear-gradient(135deg, rgba(184, 92, 56, 0.10), rgba(132, 150, 112, 0.14)),
                rgba(255, 251, 245, 0.92);
        }
        .cb-code {
            background: #1d1a17;
            color: #efe5d8;
            padding: 1rem 1.1rem;
            white-space: pre-wrap;
            font-size: 0.86rem;
            line-height: 1.5;
        }
        .cb-section-title {
            font-size: 1.35rem;
            font-weight: 700;
            letter-spacing: -0.01em;
            color: var(--cb-ink);
        }
        .cb-kicker {
            text-transform: uppercase;
            letter-spacing: 0.12em;
            font-size: 0.72rem;
            font-weight: 700;
            color: var(--cb-accent);
        }
        .cb-muted {
            color: var(--cb-muted);
        }
        .cb-chip {
            display: inline-flex;
            align-items: center;
            gap: 0.35rem;
            padding: 0.35rem 0.7rem;
            border-radius: 999px;
            background: var(--cb-accent-soft);
            color: var(--cb-accent-deep);
            font-size: 0.82rem;
            font-weight: 600;
        }
        .cb-chip-ok {
            background: rgba(47, 106, 79, 0.14);
            color: var(--cb-ok);
        }
        .cb-chip-warn {
            background: rgba(176, 107, 30, 0.14);
            color: var(--cb-warn);
        }
        .cb-chip-danger {
            background: rgba(154, 46, 59, 0.12);
            color: #9a2e3b;
        }
        .cb-table {
            overflow: auto;
        }
        .cb-status-panel {
            border-radius: 22px;
            padding: 1rem 1.1rem;
            border: 1px solid transparent;
        }
        .cb-status-running {
            background: linear-gradient(180deg, rgba(217, 243, 228, 0.95), rgba(238, 249, 241, 0.92));
            border-color: rgba(47, 106, 79, 0.16);
        }
        .cb-status-partial {
            background: linear-gradient(180deg, rgba(255, 242, 204, 0.95), rgba(255, 248, 230, 0.92));
            border-color: rgba(176, 107, 30, 0.18);
        }
        .cb-status-stopped {
            background: linear-gradient(180deg, rgba(248, 215, 218, 0.95), rgba(253, 239, 240, 0.92));
            border-color: rgba(154, 46, 59, 0.16);
        }
        .cb-stat-value {
            font-size: 1.85rem;
            font-weight: 800;
            line-height: 1;
            color: var(--cb-ink);
        }
        .cb-stat-label {
            font-size: 0.82rem;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: var(--cb-muted);
        }
        .q-card {
            border-radius: var(--cb-radius-lg);
        }
        .q-btn {
            border-radius: 14px;
            text-transform: none;
            font-weight: 700;
            letter-spacing: 0;
        }
        .q-btn.bg-primary,
        .q-btn.text-white {
            background: linear-gradient(135deg, var(--cb-accent) 0%, #cf7c50 100%) !important;
        }
        .q-field__control,
        .q-field--outlined .q-field__control {
            border-radius: 14px !important;
            background: rgba(255, 252, 246, 0.82);
        }
        .q-table {
            border-radius: 18px;
            overflow: hidden;
        }
        .q-table thead tr {
            background: rgba(184, 92, 56, 0.08);
        }
        .q-table tbody tr:nth-child(even) {
            background: rgba(255, 252, 246, 0.56);
        }
        @media (max-width: 1023px) {
            .cb-shell-nav {
                top: 0;
                position: static;
            }
        }
        @media (max-width: 767px) {
            .cb-shell-header {
                padding-left: 1rem;
                padding-right: 1rem;
            }
            .cb-card {
                border-radius: 20px;
            }
        }
        </style>
        """
    )
    state = {
        "auto_refresh": True,
        "selected_session_name": "",
        "selected_task_id": "",
        "selected_task_status": "",
        "selected_task_agent": "",
        "selected_task_backend": "",
        "session_page": 1,
        "task_page": 1,
        "agent_page": 1,
        "checks_page": 1,
        "load_session_detail": False,
        "load_task_detail": False,
        "checks_in_progress": False,
        "active_page": "home",
    }

    def refresh_model():
        model = build_web_console_view_model(
            APP_DIR,
            localizer.translate,
            page_key=state["active_page"],
            session_page=state["session_page"],
            task_page=state["task_page"],
            agent_page=state["agent_page"],
            checks_page=state["checks_page"],
            load_session_detail=state["load_session_detail"],
            load_task_detail=state["load_task_detail"],
            selected_session_name=state["selected_session_name"],
            selected_task_id=state["selected_task_id"],
            selected_task_status=state["selected_task_status"],
            selected_task_agent=state["selected_task_agent"],
            selected_task_backend=state["selected_task_backend"],
        )
        state["selected_session_name"] = model.selected_session_name
        state["selected_task_id"] = model.selected_task_id
        state["selected_task_status"] = model.selected_task_status
        state["selected_task_agent"] = model.selected_task_agent
        state["selected_task_backend"] = model.selected_task_backend
        state["session_page"] = model.session_page
        state["task_page"] = model.task_page
        state["agent_page"] = model.agent_page
        state["checks_page"] = model.checks_page
        state["checks_in_progress"] = model.checks_in_progress
        return model

    def jump_to(anchor: str) -> None:
        target = next((page for page in PRIMARY_PAGES if page.anchor == anchor or page.key == anchor), None)
        if target is not None:
            state["active_page"] = target.key
        ui.run_javascript(f"window.location.hash = '{anchor}'")
        content_view.refresh()

    def refresh_view() -> None:
        content_view.refresh()

    def should_auto_refresh() -> bool:
        if state["active_page"] in {"home", "sessions"}:
            return True
        return bool(state["checks_in_progress"])

    def notify_only(result_message: str) -> None:
        ui.notify(result_message, position="top")

    open_qr_login = install_qr_login_dialog(ui, notify_only, refresh_view)

    @ui.refreshable
    def content_view() -> None:
        model = refresh_model()
        with ui.column().classes("w-full max-w-7xl mx-auto gap-6 p-4"):
            if state["active_page"] == "home":
                render_home_section(
                    ui,
                    model,
                    _run_action,
                    _submit_task,
                    _switch_account,
                    _switch_bridge_agent,
                    _set_weixin_notice_enabled,
                    _open_weixin_binding,
                    _open_weixin_binding_task,
                    _switch_weixin_binding_backend,
                    _reset_weixin_binding,
                    _run_primary_action,
                    open_qr_login,
                    _save_agent,
                    _delete_agent,
                    _terminate_external_agent,
                    _copy_external_session_hint,
                )
            elif state["active_page"] == "issues":
                render_issues_section(ui, model, _run_repair_command)
            elif state["active_page"] == "sessions":
                render_sessions_section(
                    ui,
                    model,
                    _select_session,
                    _set_session_page,
                    _load_selected_session_detail,
                    _select_task,
                    _set_task_page,
                    _load_selected_task_detail,
                    _set_task_filters,
                    _find_task_by_id,
                    _open_weixin_binding,
                    _open_weixin_binding_task,
                    _switch_weixin_binding_backend,
                    _reset_weixin_binding,
                )
            else:
                render_diagnostics_section(
                    ui,
                    model,
                    _set_checks_page,
                    _switch_bridge_agent,
                    _set_agent_page,
                    _save_agent,
                    _delete_agent,
                    _terminate_external_agent,
                    _copy_external_session_hint,
                )

    def _notify(result_message: str) -> None:
        ui.notify(result_message, position="top")
        content_view.refresh()

    def _run_action(action: str) -> None:
        result = run_named_action(action)
        _notify(result.message)

    def _switch_account(account_id: str) -> None:
        result = switch_active_account(account_id)
        _notify(result.message)

    def _switch_bridge_agent(agent_id: str) -> None:
        result = switch_bridge_agent(agent_id)
        _notify(result.message)

    def _set_weixin_notice_enabled(service_enabled: bool, config_enabled: bool, task_enabled: bool) -> None:
        result = set_weixin_notice_enabled(service_enabled, config_enabled, task_enabled)
        _notify(result.message)

    def _submit_task(agent_id: str, prompt: str, session_name: str, backend: str) -> None:
        result = submit_hub_task(agent_id=agent_id, prompt=prompt, session_name=session_name, backend=backend)
        _notify(result.message)

    def _run_repair_command(command: str, label: str) -> None:
        result = run_repair_command(command, label)
        _notify(result.message)

    def _save_agent(
        agent_id: str,
        name: str,
        workdir: str,
        session_file: str,
        backend: str,
        model_name: str,
        prompt_prefix: str,
        enabled: bool,
    ) -> None:
        result = save_agent(agent_id, name, workdir, session_file, backend, model_name, prompt_prefix, enabled)
        _notify(result.message)

    def _delete_agent(agent_id: str) -> None:
        result = delete_agent(agent_id)
        if state["selected_task_agent"] == agent_id:
            state["selected_task_agent"] = ""
        _notify(result.message)

    def _terminate_external_agent(pid: int) -> None:
        result = terminate_external_agent(pid)
        _notify(result.message)

    def _copy_external_session_hint(session_hint: str) -> None:
        cleaned_hint = session_hint.strip()
        if not cleaned_hint:
            _notify("当前外部进程没有可复制的会话标识")
            return
        ui.run_javascript(f"navigator.clipboard.writeText({cleaned_hint!r})")
        ui.notify(f"已复制会话标识：{cleaned_hint}", position="top")

    def _run_primary_action(action_key: str) -> None:
        execute_primary_action(
            action_key,
            refresh=refresh_view,
            jump=jump_to,
            notify=_notify,
            open_qr_login=open_qr_login,
        )

    def _select_session(session_name: str) -> None:
        state["selected_session_name"] = session_name
        state["session_page"] = 1
        state["task_page"] = 1
        state["load_session_detail"] = False
        state["load_task_detail"] = False
        content_view.refresh()

    def _select_task(task_id: str, session_name: str = "") -> None:
        state["selected_task_id"] = task_id
        if session_name:
            state["selected_session_name"] = session_name
        state["load_task_detail"] = False
        content_view.refresh()

    def _set_task_filters(status: str = "", agent: str = "", backend: str = "") -> None:
        state["selected_task_status"] = status
        state["selected_task_agent"] = agent
        state["selected_task_backend"] = backend
        state["selected_task_id"] = ""
        state["task_page"] = 1
        state["load_task_detail"] = False
        content_view.refresh()

    def _set_session_page(page: int) -> None:
        state["session_page"] = max(1, int(page))
        content_view.refresh()

    def _set_task_page(page: int) -> None:
        state["task_page"] = max(1, int(page))
        content_view.refresh()

    def _set_agent_page(page: int) -> None:
        state["agent_page"] = max(1, int(page))
        content_view.refresh()

    def _set_checks_page(page: int) -> None:
        state["checks_page"] = max(1, int(page))
        content_view.refresh()

    def _find_task_by_id(task_id: str) -> None:
        cleaned_id = task_id.strip()
        if not cleaned_id:
            _notify("请输入 task_id")
            return
        model = refresh_model()
        matched = next((task for task in model.tasks if task.task_id == cleaned_id), None)
        if matched is None:
            _notify(f"最近任务中未找到：{cleaned_id}")
            return
        state["selected_task_id"] = matched.task_id
        state["selected_session_name"] = matched.session_name
        state["load_session_detail"] = False
        state["load_task_detail"] = False
        content_view.refresh()

    def _load_selected_session_detail() -> None:
        state["load_session_detail"] = True
        content_view.refresh()

    def _load_selected_task_detail() -> None:
        state["load_task_detail"] = True
        content_view.refresh()

    def _open_weixin_binding(session_name: str) -> None:
        cleaned_name = session_name.strip()
        if not cleaned_name:
            _notify("当前微信会话没有可定位的会话名")
            return
        state["active_page"] = "sessions"
        state["selected_session_name"] = cleaned_name
        state["selected_task_id"] = ""
        state["load_session_detail"] = False
        state["load_task_detail"] = False
        content_view.refresh()
        jump_to("sessions")

    def _open_weixin_binding_task(task_id: str, session_name: str) -> None:
        cleaned_task_id = task_id.strip()
        if not cleaned_task_id:
            _notify("该发送方还没有最近任务")
            return
        state["active_page"] = "sessions"
        state["selected_session_name"] = session_name.strip()
        state["selected_task_id"] = cleaned_task_id
        state["load_session_detail"] = False
        state["load_task_detail"] = False
        content_view.refresh()
        jump_to("sessions")

    def _switch_weixin_binding_backend(sender_id: str, backend: str) -> None:
        result = switch_weixin_session_backend(sender_id, backend)
        _notify(result.message)

    def _reset_weixin_binding(sender_id: str) -> None:
        result = reset_weixin_conversation(sender_id)
        _notify(result.message)

    def toggle_auto_refresh() -> None:
        state["auto_refresh"] = not state["auto_refresh"]
        if auto_refresh_button is not None:
            auto_refresh_button.text = AUTO_REFRESH_ON_ACTION.label if state["auto_refresh"] else AUTO_REFRESH_OFF_ACTION.label
        content_view.refresh()

    @ui.page("/")
    def index_page() -> None:
        nonlocal auto_refresh_button
        with ui.header().classes("cb-shell-header items-center justify-between text-slate-800 shadow-none px-5 py-4"):
            with ui.column().classes("gap-0"):
                ui.label(APP_SHELL.app_name).classes("text-3xl font-black tracking-tight")
                ui.label(APP_SHELL.app_subtitle).classes("text-sm cb-muted")
            with ui.row().classes("gap-2 items-center"):
                auto_refresh_button = ui.button(
                    AUTO_REFRESH_ON_ACTION.label if state["auto_refresh"] else AUTO_REFRESH_OFF_ACTION.label,
                    on_click=lambda: toggle_auto_refresh(),
                ).props("outline")

        with ui.row().classes("cb-shell-nav w-full gap-2 px-5 py-3 sticky top-[84px] z-40 flex-wrap"):
            for page in PRIMARY_PAGES:
                props = "color=primary unelevated" if page.key == state["active_page"] else "outline"
                icon = {
                    "home": "⌂",
                    "issues": "!",
                    "sessions": "#",
                    "diagnostics": "≈",
                }.get(page.key, "•")
                ui.button(
                    f"{icon} {page.title}",
                    on_click=lambda anchor=page.anchor: jump_to(anchor),
                ).props(props).classes(f"cb-nav-button {'cb-nav-active' if page.key == state['active_page'] else ''}")
            ui.space()
            for action in APP_SHELL.topbar_actions:
                ui.button(
                    action.label,
                    on_click=lambda key=action.key: execute_topbar_action(
                        key,
                        refresh=refresh_view,
                        jump=jump_to,
                        notify=notify_only,
                        open_qr_login=open_qr_login,
                    ),
                ).props("outline")

        content_view()
        ui.timer(2.0, lambda: content_view.refresh() if state["auto_refresh"] and should_auto_refresh() else None)

    auto_refresh_button = None


def run_ui(host: str = "0.0.0.0", port: int = 8765, native: bool = False) -> None:
    ui = _load_nicegui()
    create_ui()
    ui.run(
        host=host,
        port=port,
        reload=False,
        native=native,
        show=False,
        title=f"{APP_SHELL.app_name} UI",
    )
