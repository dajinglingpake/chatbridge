from __future__ import annotations

from pathlib import Path

from core.action_defs import AUTO_REFRESH_OFF_ACTION, AUTO_REFRESH_ON_ACTION
from core.app_service import run_named_action, run_repair_command, submit_hub_task, switch_active_account
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
    state = {"auto_refresh": True, "selected_session_name": ""}

    def refresh_model():
        model = build_web_console_view_model(
            APP_DIR,
            localizer.translate,
            selected_session_name=state["selected_session_name"],
        )
        state["selected_session_name"] = model.selected_session_name
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
            render_home_section(ui, model, _run_action, _submit_task, _switch_account, _run_primary_action, open_qr_login)
            render_issues_section(ui, model, _run_repair_command)
            render_sessions_section(ui, model, _select_session)
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

    def _submit_task(agent_id: str, prompt: str, session_name: str, backend: str) -> None:
        result = submit_hub_task(agent_id=agent_id, prompt=prompt, session_name=session_name, backend=backend)
        _notify(result.message)

    def _run_repair_command(command: str, label: str) -> None:
        result = run_repair_command(command, label)
        _notify(result.message)

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
