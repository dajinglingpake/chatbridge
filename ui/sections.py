from __future__ import annotations

from typing import Any

from agent_backends import supported_backend_options
from core.navigation import DIAGNOSTICS_PAGE, HOME_PAGE, ISSUES_PAGE, SESSIONS_PAGE
from core.view_models import WebConsoleViewModel


def render_home_section(
    ui: Any,
    model: WebConsoleViewModel,
    on_run_action,
    on_submit_task,
    on_switch_account,
    on_switch_bridge_agent,
    on_set_weixin_notice_enabled,
    on_open_weixin_binding,
    on_open_weixin_binding_task,
    on_switch_weixin_binding_backend,
    on_reset_weixin_binding,
    on_run_primary,
    on_open_qr_login,
    on_save_agent,
    on_delete_agent,
    on_terminate_external_agent,
    on_copy_external_session_hint,
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
                backend = ui.select(supported_backend_options(include_default=True), value="", label="后端")
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

            with ui.card().classes("w-full"):
                ui.label("系统通知").classes("text-lg font-semibold")
                service_notice = ui.switch("服务生命周期通知", value=model.service_notice_enabled)
                config_notice = ui.switch("配置变更通知", value=model.config_notice_enabled)
                task_notice = ui.switch("任务通知", value=model.task_notice_enabled)
                ui.label("服务生命周期：启动 / 停止 / 重启 / 紧急停止。").classes("text-sm text-slate-500")
                ui.label("配置变更：切换账号、切换微信桥默认 Agent、保存 / 删除 Agent、执行修复命令。").classes("text-sm text-slate-500")
                ui.label("任务通知：界面提交任务成功 / 失败，以及后台任务完成 / 失败结果。").classes("text-sm text-slate-500")
                ui.label("微信侧也可以直接发送 /notify 查看和切换。").classes("text-sm text-slate-500")
                ui.button(
                    "应用通知设置",
                    on_click=lambda: on_set_weixin_notice_enabled(
                        bool(service_notice.value),
                        bool(config_notice.value),
                        bool(task_notice.value),
                    ),
                )

            with ui.card().classes("w-full col-span-2"):
                ui.label("Agent 管理").classes("text-lg font-semibold")
                ui.label(f"微信桥当前默认 Agent：{model.bridge_agent_id or 'main'}").classes("text-sm text-slate-500")
                bridge_agent_options = {item.agent_id: item.label for item in model.agent_options}
                bridge_agent_select = ui.select(
                    bridge_agent_options,
                    value=model.bridge_agent_id or (model.agent_options[0].agent_id if model.agent_options else "main"),
                    label="微信桥默认 Agent",
                ).classes("w-full")
                with ui.row().classes("gap-2"):
                    ui.button("切换微信桥默认 Agent", on_click=lambda: on_switch_bridge_agent(bridge_agent_select.value or ""))
                    ui.label("切换后会自动重启 Bridge 生效。").classes("text-sm text-slate-500 self-center")
                agent_rows = [
                    {
                        "ID": item.agent_id,
                        "名称": item.name,
                        "后端": item.backend,
                        "启用": "是" if item.enabled else "否",
                        "状态": item.runtime_status,
                        "队列": item.queue_size,
                    }
                    for item in model.agent_management
                ]
                ui.table(
                    columns=[{"name": key, "label": key, "field": key} for key in ["ID", "名称", "后端", "启用", "状态", "队列"]],
                    rows=agent_rows,
                    row_key="ID",
                ).classes("w-full")

                agent_lookup = {item.agent_id: item for item in model.agent_management}
                agent_options = {"": "新建 Agent", **{item.agent_id: f"{item.name} ({item.agent_id})" for item in model.agent_management}}
                selected_agent = ui.select(agent_options, value="", label="编辑 Agent").classes("w-full")
                agent_id = ui.input(label="Agent ID", placeholder="assistant-1")
                agent_name = ui.input(label="名称", placeholder="客服助手")
                workdir = ui.input(label="工作目录", placeholder="workspace")
                session_file = ui.input(label="会话文件", placeholder="sessions/assistant-1.txt")
                backend = ui.select(supported_backend_options(), value="codex", label="后端").classes("w-full")
                model_input = ui.input(label="模型", placeholder="可选")
                prompt_prefix = ui.textarea(label="Prompt Prefix", placeholder="可选")
                enabled = ui.switch("启用", value=True)

                def fill_agent_form(agent_key: str) -> None:
                    item = agent_lookup.get(agent_key)
                    if item is None:
                        agent_id.value = ""
                        agent_name.value = ""
                        workdir.value = "workspace"
                        session_file.value = "sessions/main.txt"
                        backend.value = "codex"
                        model_input.value = ""
                        prompt_prefix.value = ""
                        enabled.value = True
                        return
                    agent_id.value = item.agent_id
                    agent_name.value = item.name
                    workdir.value = item.workdir
                    session_file.value = item.session_file
                    backend.value = item.backend
                    model_input.value = item.model
                    prompt_prefix.value = item.prompt_prefix
                    enabled.value = item.enabled

                selected_agent.on_value_change(lambda event: fill_agent_form(event.value or ""))
                fill_agent_form("")

                with ui.dialog() as delete_dialog, ui.card().classes("min-w-[28rem]"):
                    ui.label("确认删除 Agent").classes("text-lg font-semibold")
                    ui.label("删除后会移除该 Agent 配置和任务记录。若该 Agent 正被微信桥使用，Hub 会拒绝删除。").classes("text-sm text-slate-600")
                    with ui.row().classes("justify-end gap-2 w-full"):
                        ui.button("取消", on_click=delete_dialog.close).props("flat")
                        ui.button(
                            "确认删除",
                            color="negative",
                            on_click=lambda: (
                                on_delete_agent(selected_agent.value or ""),
                                delete_dialog.close(),
                            ),
                        )

                with ui.row().classes("gap-2"):
                    ui.button(
                        "保存 Agent",
                        on_click=lambda: on_save_agent(
                            agent_id.value or "",
                            agent_name.value or "",
                            workdir.value or "",
                            session_file.value or "",
                            backend.value or "",
                            model_input.value or "",
                            prompt_prefix.value or "",
                            bool(enabled.value),
                        ),
                    )
                    ui.button("重置表单", on_click=lambda: fill_agent_form(selected_agent.value or "")).props("outline")
                    ui.button(
                        "删除 Agent",
                        color="negative",
                        on_click=lambda: delete_dialog.open() if selected_agent.value else None,
                    ).props("outline")

            with ui.card().classes("w-full col-span-2"):
                ui.label("外部终端 Agent 进程").classes("text-lg font-semibold")
                ui.label("这里只显示未被 ChatBridge 接管、但当前机器上正在运行的 Codex / Claude / OpenCode 进程。").classes("text-sm text-slate-500")
                ui.label("当前支持明确区分和结束进程，不会把它们伪装成可接管会话。").classes("text-sm text-slate-500")
                if model.external_agent_processes:
                    for item in model.external_agent_processes:
                        with ui.card().classes("w-full bg-amber-50"):
                            with ui.dialog() as terminate_dialog, ui.card().classes("min-w-[28rem]"):
                                ui.label("确认结束外部 Agent 进程").classes("text-lg font-semibold")
                                ui.label(f"PID {item.pid} 将被直接结束。这个操作只影响外部终端里手动启动的 Agent 进程。").classes("text-sm text-slate-600")
                                ui.code(item.command_line).classes("w-full whitespace-pre-wrap")
                                with ui.row().classes("justify-end gap-2 w-full"):
                                    ui.button("取消", on_click=terminate_dialog.close).props("flat")
                                    ui.button(
                                        "确认结束",
                                        color="negative",
                                        on_click=lambda pid=item.pid: (
                                            on_terminate_external_agent(pid),
                                            terminate_dialog.close(),
                                        ),
                                    )
                            ui.label(f"PID {item.pid} | {item.backend} | {item.managed_label}").classes("font-semibold")
                            ui.label(f"进程名: {item.name}").classes("text-sm text-slate-700")
                            if item.session_hint:
                                ui.label(f"会话标识: {item.session_hint}").classes("text-sm text-slate-700")
                            ui.code(item.command_line).classes("w-full whitespace-pre-wrap max-h-36 overflow-auto")
                            with ui.row().classes("gap-2"):
                                if item.session_hint:
                                    ui.button(
                                        "复制会话标识",
                                        on_click=lambda session_hint=item.session_hint: on_copy_external_session_hint(session_hint),
                                    ).props("outline")
                                ui.button("结束进程", color="negative", on_click=terminate_dialog.open).props("outline")
                else:
                    ui.label("当前没有发现外部终端里手动启动的 Agent 进程。").classes("text-slate-500")

            with ui.card().classes("w-full col-span-2"):
                ui.label("微信会话绑定").classes("text-lg font-semibold")
                if model.weixin_conversations:
                    for item in model.weixin_conversations:
                        with ui.card().classes("w-full bg-stone-50"):
                            with ui.dialog() as reset_dialog, ui.card().classes("min-w-[28rem]"):
                                ui.label("确认重置微信会话").classes("text-lg font-semibold")
                                ui.label("这会删除该发送方的会话状态，并在 Bridge 运行中时自动重启使其生效。").classes("text-sm text-slate-600")
                                with ui.row().classes("justify-end gap-2 w-full"):
                                    ui.button("取消", on_click=reset_dialog.close).props("flat")
                                    ui.button(
                                        "确认重置",
                                        color="negative",
                                        on_click=lambda sender_id=item.sender_id: (
                                            on_reset_weixin_binding(sender_id),
                                            reset_dialog.close(),
                                        ),
                                    )
                            with ui.row().classes("w-full items-center justify-between gap-3"):
                                with ui.column().classes("gap-1"):
                                    ui.label(f"发送方: {item.sender_id}").classes("font-semibold")
                                    ui.label(f"Agent: {item.agent_id} | 当前会话: {item.current_session} | 当前后端: {item.current_backend}").classes("text-sm text-slate-700")
                                    ui.label(f"会话数: {item.session_count} | 最近更新: {item.updated_at}").classes("text-sm text-slate-500")
                                    if item.latest_task_id:
                                        ui.label(f"最近任务: {item.latest_task_id} [{item.latest_task_status}]").classes("text-sm text-slate-500")
                                with ui.column().classes("items-end gap-2 min-w-[15rem]"):
                                    backend_select = ui.select(
                                        supported_backend_options(),
                                        value=item.current_backend,
                                        label="当前会话后端",
                                    ).classes("w-full")
                                    with ui.row().classes("gap-2"):
                                        ui.button(
                                            "打开该会话",
                                            on_click=lambda session_name=item.current_session: on_open_weixin_binding(session_name),
                                        ).props("outline")
                                        ui.button(
                                            "打开最近任务",
                                            on_click=lambda task_id=item.latest_task_id, session_name=item.latest_task_session: on_open_weixin_binding_task(task_id, session_name),
                                        ).props("outline")
                                        ui.button(
                                            "切换后端",
                                            on_click=lambda sender_id=item.sender_id, select=backend_select: on_switch_weixin_binding_backend(sender_id, select.value or ""),
                                        )
                                        ui.button("重置会话", color="negative", on_click=reset_dialog.open).props("outline")
                else:
                    ui.label("当前还没有微信会话绑定记录。").classes("text-slate-500")
                    ui.label("当 Bridge 收到消息后，这里会显示发送方当前使用的 Agent、会话和后端。").classes("text-sm text-slate-500")


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


def render_sessions_section(ui: Any, model: WebConsoleViewModel, on_select_session, on_select_task, on_set_task_filters, on_find_task_by_id) -> None:
    with ui.element("section").props(f"id={SESSIONS_PAGE.anchor}").classes("w-full"):
        ui.label(SESSIONS_PAGE.title).classes("text-2xl font-semibold")
        ui.label(SESSIONS_PAGE.description).classes("text-slate-500")
        with ui.grid(columns=2).classes("w-full gap-4"):
            with ui.card().classes("w-full"):
                ui.label("会话概览").classes("text-lg font-semibold")
                session_options = {row.name: row.name for row in model.session_rows}
                ui.select(
                    session_options,
                    value=model.selected_session_name or None,
                    label="选择会话",
                    on_change=lambda event: on_select_session(event.value or ""),
                ).classes("w-full")
                with ui.row().classes("w-full gap-2"):
                    ui.button(
                        "全部会话",
                        on_click=lambda: on_select_session(""),
                    ).props("flat" if not model.selected_session_name else "outline").classes("text-xs")
                    for row in model.session_rows:
                        props = "outline"
                        if row.name == model.selected_session_name:
                            props = "color=primary"
                        ui.button(
                            row.name,
                            on_click=lambda session_name=row.name: on_select_session(session_name),
                        ).props(props).classes("text-xs")
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
                ui.label(f"会话详情: {model.selected_session_name or '(未选择)'}").classes("text-lg font-semibold")
                ui.code("\n".join(model.session_detail_lines)).classes("w-full whitespace-pre-wrap")
                ui.separator()
                ui.label(f"会话预览: {model.selected_session_name or '(未选择)'}").classes("text-lg font-semibold")
                ui.code("\n".join(model.session_conversation_lines)).classes("w-full whitespace-pre-wrap")

        with ui.card().classes("w-full"):
            ui.label("最近任务").classes("text-lg font-semibold")
            if model.selected_session_name:
                ui.label(f"当前按会话过滤: {model.selected_session_name}").classes("text-sm text-slate-500")
            if model.task_filtered_count != model.task_total_count:
                ui.label(f"当前显示 {model.task_filtered_count} / {model.task_total_count} 条任务").classes("text-sm text-slate-500")
            with ui.row().classes("w-full gap-2"):
                status_filter = ui.select(
                    {"": "全部状态", **{item: item for item in model.task_status_options}},
                    value=model.selected_task_status,
                    label="状态",
                ).classes("min-w-[12rem]")
                agent_filter = ui.select(
                    {"": "全部 Agent", **{item: item for item in model.task_agent_options}},
                    value=model.selected_task_agent,
                    label="Agent",
                ).classes("min-w-[14rem]")
                backend_filter = ui.select(
                    {"": "全部后端", **{item: item for item in model.task_backend_options}},
                    value=model.selected_task_backend,
                    label="后端",
                ).classes("min-w-[12rem]")
                ui.button(
                    "应用筛选",
                    on_click=lambda: on_set_task_filters(
                        status_filter.value or "",
                        agent_filter.value or "",
                        backend_filter.value or "",
                    ),
                ).props("outline")
                ui.button("清空筛选", on_click=lambda: on_set_task_filters("", "", "")).props("flat")
            with ui.row().classes("w-full gap-2"):
                task_lookup = ui.input(label="按 task_id 快速定位", placeholder="task-xxxxxxxxxx").classes("min-w-[18rem]")
                ui.button("定位任务", on_click=lambda: on_find_task_by_id(task_lookup.value or "")).props("outline")
            task_options = {
                task.task_id: f"{task.created_at} | {task.agent_name} | {task.status} | {task.session_name}"
                for task in model.tasks
                if task.task_id
            }
            ui.select(
                task_options,
                value=model.selected_task_id or None,
                label="选择任务",
                on_change=lambda event: on_select_task(
                    event.value or "",
                    next((task.session_name for task in model.tasks if task.task_id == (event.value or "")), ""),
                ),
            ).classes("w-full")
            task_rows = [
                {
                    "时间": task.created_at,
                    "Agent": task.agent_name,
                    "后端": task.backend,
                    "状态": task.status,
                    "输入": task.prompt_summary,
                    "输出/错误": task.result_summary,
                }
                for task in model.tasks
            ]
            ui.table(
                columns=[{"name": key, "label": key, "field": key} for key in ["时间", "Agent", "后端", "状态", "输入", "输出/错误"]],
                rows=task_rows,
                row_key="时间",
            ).classes("w-full")
            if not task_rows:
                ui.label("当前筛选条件下没有任务。").classes("text-sm text-slate-500")
            ui.separator()
            ui.label(f"任务详情: {model.selected_task_id or '(未选择)'}").classes("text-lg font-semibold")
            ui.code("\n".join(model.task_detail_lines)).classes("w-full whitespace-pre-wrap")
            ui.separator()
            ui.label("完整输出 / 错误").classes("text-lg font-semibold")
            ui.code("\n".join(model.task_result_lines)).classes("w-full whitespace-pre-wrap max-h-80 overflow-auto")


def render_diagnostics_section(ui: Any, model: WebConsoleViewModel) -> None:
    with ui.element("section").props(f"id={DIAGNOSTICS_PAGE.anchor}").classes("w-full"):
        ui.label(DIAGNOSTICS_PAGE.title).classes("text-2xl font-semibold")
        ui.label(DIAGNOSTICS_PAGE.description).classes("text-slate-500")
        with ui.grid(columns=2).classes("w-full gap-4"):
            with ui.card().classes("w-full"):
                rows = [
                    {
                        "项目": check.label,
                        "状态": check.status_text,
                        "详情": check.detail,
                    }
                    for check in model.checks
                ]
                ui.label("环境检查").classes("text-lg font-semibold")
                ui.table(
                    columns=[{"name": key, "label": key, "field": key} for key in ["项目", "状态", "详情"]],
                    rows=rows,
                    row_key="项目",
                ).classes("w-full")
            with ui.card().classes("w-full"):
                ui.label("运行日志").classes("text-lg font-semibold")
                for title, content in model.log_sections:
                    ui.label(title).classes("font-semibold text-slate-700")
                    ui.code(content).classes("w-full whitespace-pre-wrap max-h-60 overflow-auto")
                    ui.separator()
