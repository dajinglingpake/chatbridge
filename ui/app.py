from __future__ import annotations

from pathlib import Path

from starlette.requests import Request

from core.app_service import delete_agent, reset_weixin_conversation, run_named_action, run_repair_command, save_agent, set_weixin_notice_enabled, submit_hub_task, switch_active_account, switch_bridge_agent, switch_weixin_session_backend, terminate_external_agent
from core.navigation import PRIMARY_PAGES
from core.shell_schema import APP_SHELL
from core.dashboard import refresh_dashboard_cache
from core.view_models import build_web_console_view_model
from localization import Localizer, normalize_language
from ui.qr_login import install_qr_login_dialog
from ui.sections import render_diagnostics_section, render_home_section, render_sessions_section


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
    localizer_ref = {"value": Localizer()}

    def translate(key: str, **kwargs: object) -> str:
        return localizer_ref["value"].translate(key, **kwargs)

    def t(key: str, fallback: str = "", **kwargs: object) -> str:
        value = translate(key, **kwargs)
        return value if value != key else fallback.format(**kwargs)

    def page_label(page_key: str, fallback: str) -> str:
        return {
            "home": t("ui.tab.home", fallback),
            "sessions": t("ui.tab.sessions", fallback),
            "diagnostics": t("ui.tab.logs", fallback),
        }.get(page_key, fallback)

    ui.add_head_html(
        """
        <style>
        :root {
            --cb-bg: #f6f7f9;
            --cb-surface: #ffffff;
            --cb-surface-muted: #f1f5f4;
            --cb-surface-raised: #fbfcfd;
            --cb-border: #dce2e6;
            --cb-border-strong: #c5cdd3;
            --cb-ink: #172026;
            --cb-muted: #65717b;
            --cb-accent: #176b7a;
            --cb-accent-deep: #0d4f5c;
            --cb-accent-soft: #e1f1f3;
            --cb-ok: #23704f;
            --cb-warn: #a16207;
            --cb-danger: #b4233b;
            --cb-shadow: 0 1px 2px rgba(15, 23, 42, 0.06);
            --cb-radius: 8px;
        }
        body {
            background: var(--cb-bg);
            color: var(--cb-ink);
            font-size: 14px;
        }
        .nicegui-content {
            background: transparent !important;
        }
        .cb-shell-header {
            background: var(--cb-surface);
            border-bottom: 1px solid var(--cb-border);
            min-height: 64px;
        }
        .cb-shell-nav {
            background: transparent;
            border-bottom: 0;
        }
        .cb-nav-button {
            min-width: 6rem;
            height: 2.25rem;
        }
        .cb-nav-button.q-btn--outline {
            border-color: var(--cb-border);
            color: var(--cb-muted);
            background: var(--cb-surface);
        }
        .cb-nav-button.cb-nav-active {
            background: var(--cb-accent) !important;
            color: #ffffff !important;
            border-color: var(--cb-accent) !important;
        }
        .cb-card,
        .cb-panel,
        .cb-code {
            border-radius: var(--cb-radius);
            border: 1px solid var(--cb-border);
            box-shadow: var(--cb-shadow);
        }
        .cb-card {
            background: var(--cb-surface);
        }
        .cb-panel {
            background: var(--cb-surface-raised);
        }
        .cb-hero {
            background: #10252b;
            border-color: #10252b;
            color: #ffffff;
        }
        .cb-code {
            background: #111827;
            color: #e5e7eb;
            padding: 0.85rem 1rem;
            white-space: pre-wrap;
            font-size: 0.82rem;
            line-height: 1.5;
        }
        .cb-hero .cb-code {
            background: rgba(255, 255, 255, 0.08);
            border-color: rgba(255, 255, 255, 0.14);
            color: #d8eef2;
            box-shadow: none;
        }
        .cb-hero .cb-panel {
            background: rgba(255, 255, 255, 0.07);
            border-color: rgba(255, 255, 255, 0.14);
            color: #eef8fa;
            box-shadow: none;
        }
        .cb-section-title {
            font-size: 1rem;
            font-weight: 700;
            color: var(--cb-ink);
        }
        .cb-kicker {
            text-transform: uppercase;
            letter-spacing: 0;
            font-size: 0.75rem;
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
            padding: 0.25rem 0.55rem;
            border-radius: 999px;
            background: var(--cb-accent-soft);
            color: var(--cb-accent-deep);
            font-size: 0.78rem;
            font-weight: 600;
        }
        .cb-chip-ok {
            background: #e5f5ec;
            color: var(--cb-ok);
        }
        .cb-chip-warn {
            background: #fff3d7;
            color: var(--cb-warn);
        }
        .cb-chip-danger {
            background: #fde7eb;
            color: var(--cb-danger);
        }
        .cb-table {
            overflow: auto;
        }
        .cb-status-panel {
            border-radius: var(--cb-radius);
            padding: 0.9rem 1rem;
            border: 1px solid var(--cb-border);
        }
        .cb-status-running {
            background: #eef8f2;
            border-color: #b7dfc9;
        }
        .cb-status-partial {
            background: #fff8e5;
            border-color: #f2d487;
        }
        .cb-status-stopped {
            background: #fff0f2;
            border-color: #efb4bd;
        }
        .cb-stat-value {
            font-size: 1.45rem;
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
            border-radius: var(--cb-radius);
        }
        .q-btn {
            border-radius: 6px;
            text-transform: none;
            font-weight: 700;
            letter-spacing: 0;
            min-height: 2.25rem;
        }
        .q-btn.bg-primary {
            background: var(--cb-accent) !important;
        }
        .q-field__control,
        .q-field--outlined .q-field__control {
            border-radius: 6px !important;
            background: #ffffff;
        }
        .q-table {
            border-radius: 6px;
            overflow: hidden;
        }
        .q-table thead tr {
            background: var(--cb-surface-muted);
        }
        .q-table tbody tr:nth-child(even) {
            background: #fafbfc;
        }
        .q-tab-panels {
            border: 0 !important;
        }
        .cb-page-title {
            font-size: 1.35rem;
            line-height: 1.2;
            font-weight: 800;
        }
        .cb-toolbar-button {
            height: 2.25rem;
        }
        .cb-language-toggle {
            border: 1px solid var(--cb-border);
            border-radius: 6px;
            overflow: hidden;
        }
        .cb-language-toggle .q-btn {
            min-height: 2.25rem;
        }
        .cb-language-toggle .q-btn[aria-pressed="true"],
        .cb-language-toggle .q-btn.q-btn--active,
        .cb-language-toggle .q-btn.bg-primary {
            background: var(--cb-accent) !important;
            color: #fff !important;
        }
        .cb-disclosure summary {
            cursor: pointer;
            list-style: none;
        }
        .cb-disclosure summary::-webkit-details-marker {
            display: none;
        }
        .cb-disclosure summary::after {
            content: "expand_more";
            font-family: "Material Icons";
            font-size: 1.25rem;
            color: var(--cb-muted);
        }
        .cb-disclosure[open] summary::after {
            content: "expand_less";
        }
        @media (max-width: 767px) {
            .cb-shell-header {
                padding-left: 1rem;
                padding-right: 1rem;
            }
            .cb-card {
                border-radius: var(--cb-radius);
            }
        }
        </style>
        """,
        shared=True,
    )
    state = {
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
        "language": localizer_ref["value"].language,
        "qr_login_open": False,
    }

    def refresh_model():
        model = build_web_console_view_model(
            APP_DIR,
            translate,
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
        nav_view.refresh()
        content_view.refresh()

    def refresh_after_qr_login() -> None:
        content_view.refresh()

    def notify_only(result_message: str) -> None:
        ui.notify(result_message, position="top")

    def switch_language(language: str) -> None:
        selected = normalize_language(str(language or "").strip())
        if selected not in {"zh-CN", "en-US"}:
            return
        state["language"] = selected
        localizer_ref["value"] = Localizer(selected)
        ui.run_javascript(f"window.location.href = '/?lang={selected}'")

    def apply_request_language(request) -> None:
        selected = normalize_language(str(request.query_params.get("lang") or ""))
        if selected in {"zh-CN", "en-US"} and selected != state["language"]:
            state["language"] = selected
            localizer_ref["value"] = Localizer(selected)

    def mark_qr_login_open() -> None:
        state["qr_login_open"] = True

    def mark_qr_login_closed() -> None:
        state["qr_login_open"] = False
        content_view.refresh()

    open_qr_login = install_qr_login_dialog(
        ui,
        notify_only,
        refresh_after_qr_login,
        translate,
        on_open=mark_qr_login_open,
        on_close=mark_qr_login_closed,
    )

    @ui.refreshable
    def content_view() -> None:
        model = refresh_model()
        with ui.column().classes("w-full max-w-7xl mx-auto gap-6 p-4"):
            if state["active_page"] == "home":
                render_home_section(
                    ui,
                    model,
                    translate,
                    _run_action,
                    _refresh_checks,
                    _submit_task,
                    _switch_account,
                    _set_weixin_notice_enabled,
                    open_qr_login,
                )
            elif state["active_page"] == "sessions":
                render_sessions_section(
                    ui,
                    model,
                    translate,
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
                    translate,
                    _refresh_checks,
                    _refresh_logs,
                    _refresh_external_agents,
                    _set_checks_page,
                    _switch_bridge_agent,
                    _set_agent_page,
                    _save_agent,
                    _delete_agent,
                    _terminate_external_agent,
                    _copy_external_session_hint,
                    _run_repair_command,
                )

    def _notify(result_message: str) -> None:
        ui.notify(result_message, position="top")
        content_view.refresh()

    def _run_action(action: str) -> None:
        result = run_named_action(action)
        _notify(result.message)

    def _switch_account(account_id: str) -> None:
        result = switch_active_account(account_id, restart_if_running=False)
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

    def _refresh_checks() -> None:
        key = "checks_full" if state["active_page"] == "diagnostics" else "checks_light"
        refresh_dashboard_cache(APP_DIR, key)
        _notify(t("ui.web.notify.checks_refreshed", "环境检查已刷新"))

    def _refresh_logs() -> None:
        refresh_dashboard_cache(APP_DIR, "logs")
        _notify(t("ui.web.notify.logs_refreshed", "运行日志已刷新"))

    def _refresh_external_agents() -> None:
        refresh_dashboard_cache(APP_DIR, "external_agent_processes")
        _notify(t("ui.web.notify.external_agents_refreshed", "外部进程列表已刷新"))

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
            _notify(t("ui.web.notify.no_session_hint", "当前外部进程没有可复制的会话标识"))
            return
        ui.run_javascript(f"navigator.clipboard.writeText({cleaned_hint!r})")
        ui.notify(t("ui.web.notify.session_hint_copied", "已复制会话标识：{hint}", hint=cleaned_hint), position="top")

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
            _notify(t("ui.web.notify.enter_task_id", "请输入 task_id"))
            return
        model = refresh_model()
        matched = next((task for task in model.tasks if task.task_id == cleaned_id), None)
        if matched is None:
            _notify(t("ui.web.notify.task_not_found", "最近任务中未找到：{task_id}", task_id=cleaned_id))
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
            _notify(t("ui.web.notify.no_binding_session", "当前微信会话没有可定位的会话名"))
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
            _notify(t("ui.web.notify.no_latest_task", "该发送方还没有最近任务"))
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

    @ui.refreshable
    def nav_view() -> None:
        with ui.row().classes("cb-shell-nav w-full gap-2 items-center flex-wrap"):
            for page in PRIMARY_PAGES:
                active = page.key == state["active_page"]
                props = "color=primary text-color=white unelevated" if active else "outline"
                icon = {
                    "home": "dashboard",
                    "sessions": "forum",
                    "diagnostics": "monitor_heart",
                }.get(page.key, "radio_button_unchecked")
                ui.button(
                    page_label(page.key, page.title),
                    on_click=lambda anchor=page.anchor: jump_to(anchor),
                    icon=icon,
                ).props(props).classes(f"cb-nav-button {'cb-nav-active' if active else ''}")
            ui.space()
            ui.label(t("ui.web.field.language", "语言")).classes("text-sm cb-muted")
            ui.toggle(
                {"zh-CN": "中文", "en-US": "English"},
                value=state["language"],
                on_change=lambda event: switch_language(str(event.value or "")),
                clearable=False,
            ).props("unelevated color=white text-color=primary toggle-color=primary toggle-text-color=white").classes("cb-language-toggle")

    def shell_view() -> None:
        with ui.header().classes("cb-shell-header text-slate-800 shadow-none px-5 py-3"):
            nav_view()

    @ui.page("/")
    def index_page(request: Request) -> None:
        apply_request_language(request)
        shell_view()
        content_view()


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
