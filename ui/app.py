from __future__ import annotations

from pathlib import Path

from core.action_defs import AUTO_REFRESH_OFF_ACTION, AUTO_REFRESH_ON_ACTION
from core.app_service import delete_agent, reset_weixin_conversation, run_named_action, run_repair_command, save_agent, set_weixin_notice_enabled, submit_hub_task, switch_active_account, switch_bridge_agent, switch_weixin_session_backend, terminate_external_agent
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
        raise SystemExit("Missing dependency: nicegui. Run `python3 -m pip install nicegui` first.") from exc
    return ui


def create_ui() -> None:
    ui = _load_nicegui()
    localizer = Localizer()
    state = {
        "auto_refresh": True,
        "selected_session_name": "",
        "selected_task_id": "",
        "selected_task_status": "",
        "selected_task_agent": "",
        "selected_task_backend": "",
    }

    def refresh_model():
        model = build_web_console_view_model(
            APP_DIR,
            localizer.translate,
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
        return model

    def jump_to(anchor: str) -> None:
        ui.run_javascript(f"window.location.hash = '{anchor}'")

    def refresh_view() -> None:
        shell_view.refresh()

    def notify_only(result_message: str) -> None:
        ui.notify(result_message, position="top")

    open_qr_login = install_qr_login_dialog(ui, notify_only, refresh_view)

    @ui.refreshable
    def shell_view() -> None:
        model = refresh_model()
        with ui.header().classes("items-center justify-between bg-stone-100 text-slate-800 shadow-sm px-4 py-3"):
            with ui.column().classes("gap-0"):
                ui.label(APP_SHELL.app_name).classes("text-2xl font-bold")
                ui.label(APP_SHELL.app_subtitle).classes("text-sm text-slate-600")
            with ui.row().classes("gap-2 items-center"):
                ui.button(AUTO_REFRESH_ON_ACTION.label if state["auto_refresh"] else AUTO_REFRESH_OFF_ACTION.label, on_click=lambda: toggle_auto_refresh()).props("outline")

        with ui.row().classes("w-full gap-2 px-4 py-3 bg-stone-50 border-b border-stone-200 sticky top-[72px] z-40"):
            for page in APP_SHELL.pages:
                ui.link(page.title, f"#{page.anchor}").classes("rounded-full px-4 py-2 bg-white border border-stone-200 text-slate-700 no-underline")
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

        with ui.column().classes("w-full max-w-7xl mx-auto gap-6 p-4"):
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
            render_issues_section(ui, model, _run_repair_command)
            render_sessions_section(ui, model, _select_session, _select_task, _set_task_filters, _find_task_by_id)
            render_diagnostics_section(ui, model)

    def _notify(result_message: str) -> None:
        ui.notify(result_message, position="top")
        shell_view.refresh()

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
        shell_view.refresh()

    def _select_task(task_id: str, session_name: str = "") -> None:
        state["selected_task_id"] = task_id
        if session_name:
            state["selected_session_name"] = session_name
        shell_view.refresh()

    def _set_task_filters(status: str = "", agent: str = "", backend: str = "") -> None:
        state["selected_task_status"] = status
        state["selected_task_agent"] = agent
        state["selected_task_backend"] = backend
        state["selected_task_id"] = ""
        shell_view.refresh()

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
        shell_view.refresh()

    def _open_weixin_binding(session_name: str) -> None:
        cleaned_name = session_name.strip()
        if not cleaned_name:
            _notify("当前微信会话没有可定位的会话名")
            return
        state["selected_session_name"] = cleaned_name
        state["selected_task_id"] = ""
        shell_view.refresh()
        jump_to("sessions")

    def _open_weixin_binding_task(task_id: str, session_name: str) -> None:
        cleaned_task_id = task_id.strip()
        if not cleaned_task_id:
            _notify("该发送方还没有最近任务")
            return
        state["selected_session_name"] = session_name.strip()
        state["selected_task_id"] = cleaned_task_id
        shell_view.refresh()
        jump_to("sessions")

    def _switch_weixin_binding_backend(sender_id: str, backend: str) -> None:
        result = switch_weixin_session_backend(sender_id, backend)
        _notify(result.message)

    def _reset_weixin_binding(sender_id: str) -> None:
        result = reset_weixin_conversation(sender_id)
        _notify(result.message)

    def toggle_auto_refresh() -> None:
        state["auto_refresh"] = not state["auto_refresh"]
        shell_view.refresh()

    shell_view()
    ui.timer(8.0, lambda: shell_view.refresh() if state["auto_refresh"] else None)


def run_ui(host: str = "127.0.0.1", port: int = 8765, native: bool = False) -> None:
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
