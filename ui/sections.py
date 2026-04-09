from __future__ import annotations

from typing import Any

from core.navigation import DIAGNOSTICS_PAGE, HOME_PAGE, ISSUES_PAGE, SESSIONS_PAGE
from core.view_models import WebConsoleViewModel


def render_home_section(
    ui: Any,
    model: WebConsoleViewModel,
    on_run_action,
    on_submit_task,
    on_switch_account,
    on_run_primary,
    on_open_qr_login,
) -> None:
    with ui.element("section").props(f"id={HOME_PAGE.anchor}").classes("w-full"):
        ui.label(HOME_PAGE.title).classes("text-2xl font-semibold")
        ui.label(HOME_PAGE.description).classes("text-slate-500")
        with ui.grid(columns=2).classes("w-full gap-4"):
            with ui.card().classes("w-full"):
                ui.label("运行状态").classes("text-lg font-semibold")
                ui.label(model.home.badge_text).classes("text-base font-medium")
                ui.code(model.home.overview_text).classes("w-full whitespace-pre-wrap")
                with ui.row().classes("gap-2"):
                    ui.button("启动服务", on_click=lambda: on_run_action("start"))
                    ui.button("停止服务", on_click=lambda: on_run_action("stop"))
                    ui.button("重启服务", on_click=lambda: on_run_action("restart"))
                    ui.button("紧急停止", on_click=lambda: on_run_action("emergency-stop"), color="negative")

            with ui.card().classes("w-full"):
                ui.label("当前建议").classes("text-lg font-semibold")
                ui.label(model.home.summary_text).classes("font-medium")
                ui.label(f"{model.home.primary_label} ({model.home.primary_action})").classes("text-slate-700")
                ui.label(model.home.primary_hint).classes("text-slate-500")
                ui.code(model.home.quickstart_text).classes("w-full whitespace-pre-wrap")
                ui.button(model.home.primary_label, on_click=lambda: on_run_primary(model.home.primary_action))

            with ui.card().classes("w-full"):
                ui.label("提交任务").classes("text-lg font-semibold")
                agent_options = {item.agent_id: item.label for item in model.agent_options}
                agent = ui.select(
                    agent_options,
                    value=model.agent_options[0].agent_id if model.agent_options else "main",
                    label="Agent",
                )
                backend = ui.select({"": "跟随 Agent 默认配置", "codex": "codex", "opencode": "opencode"}, value="", label="后端")
                session_name = ui.input(label="会话名", placeholder="default")
                prompt = ui.textarea(label="Prompt", placeholder="输入要发给 Agent 的内容")
                ui.button(
                    "提交到 Hub",
                    on_click=lambda: on_submit_task(
                        agent.value or "main",
                        prompt.value or "",
                        session_name.value or "",
                        backend.value or "",
                    ),
                )

            with ui.card().classes("w-full"):
                ui.label("账号管理").classes("text-lg font-semibold")
                ui.label(f"当前激活账号：{model.active_account_id}")
                account_options = {item.account_id: item.label for item in model.account_options}
                account_select = ui.select(
                    account_options,
                    value=model.active_account_id or None,
                    label="切换账号",
                )
                with ui.row().classes("gap-2"):
                    ui.button("切换当前账号", on_click=lambda: on_switch_account(account_select.value or ""))
                    ui.button("扫码登录微信", on_click=on_open_qr_login).props("outline")


def render_issues_section(ui: Any, model: WebConsoleViewModel, on_run_repair_command) -> None:
    with ui.element("section").props(f"id={ISSUES_PAGE.anchor}").classes("w-full"):
        ui.label(ISSUES_PAGE.title).classes("text-2xl font-semibold")
        ui.label(ISSUES_PAGE.description).classes("text-slate-500")
        with ui.card().classes("w-full"):
            if model.issues:
                for item in model.issues:
                    ui.label(item.title).classes("text-lg font-semibold")
                    ui.code(item.detail).classes("w-full whitespace-pre-wrap")
            else:
                ui.label("当前没有需要手动处理的异常。").classes("text-slate-600")
            if model.repair_commands:
                ui.separator()
                ui.label("修复建议").classes("text-lg font-semibold")
                for item in model.repair_commands:
                    with ui.card().classes("w-full bg-stone-50"):
                        ui.label(item.label).classes("font-semibold")
                        ui.code(item.command).classes("w-full whitespace-pre-wrap")
                        if item.runnable:
                            ui.button("执行修复", on_click=lambda cmd=item.command, label=item.label: on_run_repair_command(cmd, label))
                        else:
                            ui.label("当前平台下这条修复建议需要手动执行。").classes("text-sm text-slate-500")


def render_sessions_section(ui: Any, model: WebConsoleViewModel) -> None:
    with ui.element("section").props(f"id={SESSIONS_PAGE.anchor}").classes("w-full"):
        ui.label(SESSIONS_PAGE.title).classes("text-2xl font-semibold")
        ui.label(SESSIONS_PAGE.description).classes("text-slate-500")
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


def render_diagnostics_section(ui: Any, model: WebConsoleViewModel) -> None:
    with ui.element("section").props(f"id={DIAGNOSTICS_PAGE.anchor}").classes("w-full"):
        ui.label(DIAGNOSTICS_PAGE.title).classes("text-2xl font-semibold")
        ui.label(DIAGNOSTICS_PAGE.description).classes("text-slate-500")
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
