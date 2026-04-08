from __future__ import annotations

from pathlib import Path

from core.app_service import run_named_action, submit_hub_task, switch_active_account
from core.shell_schema import APP_SHELL
from core.view_models import build_web_console_view_model
from localization import Localizer


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

    def refresh_model():
        return build_web_console_view_model(APP_DIR, localizer.translate)

    @ui.refreshable
    def shell_view() -> None:
        model = refresh_model()
        with ui.header().classes("items-center justify-between bg-stone-100 text-slate-800 shadow-sm"):
            with ui.column().classes("gap-0"):
                ui.label(APP_SHELL.app_name).classes("text-2xl font-bold")
                ui.label(APP_SHELL.app_subtitle).classes("text-sm text-slate-600")

        with ui.row().classes("w-full gap-2 px-4 py-3 bg-stone-50 border-b border-stone-200"):
            for page in APP_SHELL.pages:
                ui.link(page.title, f"#{page.anchor}").classes("rounded-full px-4 py-2 bg-white border border-stone-200 text-slate-700 no-underline")

        with ui.column().classes("w-full max-w-7xl mx-auto gap-6 p-4"):
            with ui.element("section").props("id=home").classes("w-full"):
                ui.label(APP_SHELL.pages[0].title).classes("text-2xl font-semibold")
                ui.label(APP_SHELL.pages[0].description).classes("text-slate-500")
                with ui.grid(columns=2).classes("w-full gap-4"):
                    with ui.card().classes("w-full"):
                        ui.label("运行状态").classes("text-lg font-semibold")
                        ui.label(model.home.badge_text).classes("text-base font-medium")
                        ui.code(model.home.overview_text).classes("w-full whitespace-pre-wrap")
                        with ui.row().classes("gap-2"):
                            ui.button("启动服务", on_click=lambda: _run_action("start"))
                            ui.button("停止服务", on_click=lambda: _run_action("stop"))
                            ui.button("重启服务", on_click=lambda: _run_action("restart"))
                            ui.button("紧急停止", on_click=lambda: _run_action("emergency-stop"), color="negative")
                    with ui.card().classes("w-full"):
                        ui.label("当前建议").classes("text-lg font-semibold")
                        ui.label(model.home.summary_text).classes("font-medium")
                        ui.label(f"{model.home.primary_label} ({model.home.primary_action})").classes("text-slate-700")
                        ui.label(model.home.primary_hint).classes("text-slate-500")
                        ui.code(model.home.quickstart_text).classes("w-full whitespace-pre-wrap")
                    with ui.card().classes("w-full"):
                        ui.label("提交任务").classes("text-lg font-semibold")
                        agent = ui.select({item.agent_id: item.label for item in model.agent_options}, value=model.agent_options[0].agent_id if model.agent_options else "main", label="Agent")
                        backend = ui.select({"": "跟随 Agent 默认配置", "codex": "codex", "opencode": "opencode"}, value="", label="后端")
                        session_name = ui.input(label="会话名", placeholder="default")
                        prompt = ui.textarea(label="Prompt", placeholder="输入要发给 Agent 的内容")
                        ui.button(
                            "提交到 Hub",
                            on_click=lambda: _submit_task(agent.value or "main", prompt.value or "", session_name.value or "", backend.value or ""),
                        )
                    with ui.card().classes("w-full"):
                        ui.label("账号管理").classes("text-lg font-semibold")
                        ui.label(f"当前激活账号：{model.active_account_id}")
                        account_select = ui.select(
                            {item.account_id: item.label for item in model.account_options},
                            value=model.active_account_id or None,
                            label="切换账号",
                        )
                        ui.button("切换当前账号", on_click=lambda: _switch_account(account_select.value or ""))

            with ui.element("section").props("id=issues").classes("w-full"):
                ui.label(APP_SHELL.pages[1].title).classes("text-2xl font-semibold")
                ui.label(APP_SHELL.pages[1].description).classes("text-slate-500")
                with ui.card().classes("w-full"):
                    if model.issues:
                        for item in model.issues:
                            ui.label(item.title).classes("text-lg font-semibold")
                            ui.code(item.detail).classes("w-full whitespace-pre-wrap")
                    else:
                        ui.label("当前没有需要手动处理的异常。").classes("text-slate-600")
                    if model.repair_lines:
                        ui.separator()
                        ui.label("修复建议").classes("text-lg font-semibold")
                        ui.code("\n".join(model.repair_lines)).classes("w-full whitespace-pre-wrap")

            with ui.element("section").props("id=sessions").classes("w-full"):
                ui.label(APP_SHELL.pages[2].title).classes("text-2xl font-semibold")
                ui.label(APP_SHELL.pages[2].description).classes("text-slate-500")
                with ui.grid(columns=2).classes("w-full gap-4"):
                    with ui.card().classes("w-full"):
                        ui.label("会话概览").classes("text-lg font-semibold")
                        rows = [
                            {
                                "会话": row.name,
                                "状态": row.status,
                                "队列": row.queue_size,
                                "成功": row.success_count,
                                "失败": row.failure_count,
                            }
                            for row in model.session_rows
                        ]
                        ui.table(
                            columns=[{"name": key, "label": key, "field": key} for key in ["会话", "状态", "队列", "成功", "失败"]],
                            rows=rows,
                            row_key="会话",
                        ).classes("w-full")
                    with ui.card().classes("w-full"):
                        ui.label("默认会话详情").classes("text-lg font-semibold")
                        ui.code("\n".join(model.session_detail_lines)).classes("w-full whitespace-pre-wrap")
                        ui.separator()
                        ui.label("默认会话预览").classes("text-lg font-semibold")
                        ui.code("\n".join(model.session_conversation_lines)).classes("w-full whitespace-pre-wrap")
                with ui.card().classes("w-full"):
                    ui.label("最近任务").classes("text-lg font-semibold")
                    task_rows = [
                        {
                            "时间": task.created_at,
                            "Agent": task.agent_name,
                            "后端": task.backend,
                            "状态": task.status,
                            "输入": task.prompt,
                            "输出/错误": task.result_text,
                        }
                        for task in model.tasks
                    ]
                    ui.table(
                        columns=[{"name": key, "label": key, "field": key} for key in ["时间", "Agent", "后端", "状态", "输入", "输出/错误"]],
                        rows=task_rows,
                        row_key="时间",
                    ).classes("w-full")

            with ui.element("section").props("id=diagnostics").classes("w-full"):
                ui.label(APP_SHELL.pages[3].title).classes("text-2xl font-semibold")
                ui.label(APP_SHELL.pages[3].description).classes("text-slate-500")
                with ui.card().classes("w-full"):
                    rows = [
                        {
                            "项目": check.label,
                            "状态": check.status_text,
                            "详情": check.detail,
                        }
                        for check in model.checks
                    ]
                    ui.table(
                        columns=[{"name": key, "label": key, "field": key} for key in ["项目", "状态", "详情"]],
                        rows=rows,
                        row_key="项目",
                    ).classes("w-full")

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

    shell_view()


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
