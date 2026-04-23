from __future__ import annotations

from typing import Callable, Protocol, Self

from agent_backends import supported_backend_options
from core.navigation import DIAGNOSTICS_PAGE, HOME_PAGE, ISSUES_PAGE, SESSIONS_PAGE
from core.view_models import WebConsoleViewModel


class UIEventLike(Protocol):
    value: object


class UIElementLike(Protocol):
    value: object
    text: str

    def __enter__(self) -> Self: ...
    def __exit__(self, exc_type, exc, tb) -> bool | None: ...
    def classes(self, value: str) -> Self: ...
    def props(self, value: str) -> Self: ...
    def set_enabled(self, value: bool) -> Self: ...
    def set_source(self, value: str) -> Self: ...
    def on_value_change(self, handler: Callable[[UIEventLike], None]) -> Self: ...
    def set_value(self, value: object) -> Self: ...
    def open(self) -> None: ...
    def close(self) -> None: ...
    def deactivate(self) -> None: ...


class UIFactoryLike(Protocol):
    def column(self) -> UIElementLike: ...
    def row(self) -> UIElementLike: ...
    def card(self) -> UIElementLike: ...
    def label(self, text: str = "") -> UIElementLike: ...
    def code(self, content: str) -> UIElementLike: ...
    def element(self, tag: str) -> UIElementLike: ...
    def button(self, text: str, on_click=None, **kwargs) -> UIElementLike: ...
    def tabs(self) -> UIElementLike: ...
    def tab(self, name: str, *, label: str = "") -> UIElementLike: ...
    def tab_panels(self, tab_bar: UIElementLike, *, value: UIElementLike) -> UIElementLike: ...
    def tab_panel(self, name: str) -> UIElementLike: ...
    def textarea(self, *, label: str = "", placeholder: str = "") -> UIElementLike: ...
    def select(self, options, *, value=None, label: str = "", on_change=None) -> UIElementLike: ...
    def input(self, *, label: str = "", placeholder: str = "") -> UIElementLike: ...
    def switch(self, text: str, *, value: bool = False) -> UIElementLike: ...
    def table(self, *, columns, rows, row_key: str) -> UIElementLike: ...
    def dialog(self) -> UIElementLike: ...
    def separator(self) -> UIElementLike: ...


def _render_page_intro(ui: UIFactoryLike, title: str, description: str, kicker: str) -> None:
    with ui.column().classes("gap-1 mb-1"):
        ui.label(kicker).classes("cb-kicker")
        ui.label(title).classes("text-3xl font-black tracking-tight text-slate-900")
        ui.label(description).classes("text-base cb-muted max-w-3xl")


def _render_card_title(ui: UIFactoryLike, title: str, detail: str = "") -> None:
    with ui.column().classes("gap-1 mb-3"):
        ui.label(title).classes("cb-section-title")
        if detail:
            ui.label(detail).classes("text-sm cb-muted")


def _render_code_block(ui: UIFactoryLike, content: str, extra_classes: str = "") -> None:
    ui.code(content or "暂无数据").classes(f"cb-code w-full {extra_classes}".strip())


def _responsive_grid(ui: UIFactoryLike, classes: str) -> UIElementLike:
    return ui.element("div").classes(f"grid w-full gap-4 {classes}".strip())


def _render_meta_line(ui: UIFactoryLike, text: str) -> None:
    ui.label(text).classes("text-sm cb-muted")


def _status_variant(text: str) -> tuple[str, str]:
    if "运行" in text:
        return "cb-status-running", "cb-chip cb-chip-ok"
    if "部分" in text:
        return "cb-status-partial", "cb-chip cb-chip-warn"
    return "cb-status-stopped", "cb-chip cb-chip-danger"


def _severity_variant(text: str) -> tuple[str, str]:
    lowered = text.lower()
    if any(keyword in lowered for keyword in ("失败", "缺失", "错误", "未就绪", "异常")):
        return "cb-chip cb-chip-danger", "高风险"
    if any(keyword in lowered for keyword in ("等待", "部分", "建议", "手动", "进行中")):
        return "cb-chip cb-chip-warn", "需关注"
    return "cb-chip cb-chip-ok", "正常"


def _render_session_summary_cards(ui: UIFactoryLike, model: WebConsoleViewModel, on_select_session) -> None:
    with _responsive_grid(ui, "grid-cols-1 lg:grid-cols-2"):
        for row in model.session_rows:
            selected = row.name == model.selected_session_name
            card_classes = "cb-soft-card w-full p-4 shadow-none border-2 border-[var(--cb-accent)]" if selected else "cb-soft-card w-full p-4 shadow-none"
            with ui.card().classes(card_classes):
                with ui.row().classes("w-full items-start justify-between gap-3 flex-wrap"):
                    with ui.column().classes("gap-1 grow"):
                        ui.label(row.name).classes("text-lg font-bold text-slate-900 break-all")
                        _render_meta_line(ui, f"状态: {row.status}")
                    ui.button(
                        "查看会话",
                        on_click=lambda session_name=row.name: on_select_session(session_name),
                    ).props("color=primary unelevated" if selected else "outline")
                with ui.row().classes("gap-2 flex-wrap pt-2"):
                    for label, value in (("队列", row.queue_size), ("成功", row.success_count), ("失败", row.failure_count)):
                        with ui.card().classes("bg-white/70 w-auto min-w-[5.5rem] px-3 py-2 shadow-none"):
                            ui.label(label).classes("cb-stat-label")
                            ui.label(str(value)).classes("text-base font-bold text-slate-900")


def _render_task_summary_cards(ui: UIFactoryLike, model: WebConsoleViewModel, on_select_task) -> None:
    with ui.column().classes("w-full gap-3"):
        for task in model.tasks:
            with ui.card().classes("cb-soft-card w-full p-4 shadow-none"):
                with ui.row().classes("w-full items-start justify-between gap-3 flex-wrap"):
                    with ui.column().classes("gap-1 grow"):
                        ui.label(f"{task.agent_name} / {task.status}").classes("text-base font-bold text-slate-900")
                        _render_meta_line(ui, f"{task.created_at} | 后端: {task.backend} | 会话: {task.session_name or '(未归类)'}")
                        ui.label(task.prompt_summary).classes("text-sm text-slate-800")
                        ui.label(task.result_summary).classes("text-sm cb-muted")
                    ui.button(
                        "查看任务",
                        on_click=lambda task_id=task.task_id, session_name=task.session_name: on_select_task(task_id, session_name),
                    ).props("color=primary unelevated" if task.task_id == model.selected_task_id else "outline")


def _render_detail_tabs(ui: UIFactoryLike, tabs: list[tuple[str, str, str]], code_classes: str = "") -> None:
    with ui.tabs().classes("w-full") as tab_bar:
        tab_items = []
        for name, label, _content in tabs:
            tab_items.append(ui.tab(name, label=label))
    tab_bar.set_value(tab_items[0])
    with ui.tab_panels(tab_bar, value=tab_items[0]).classes("w-full bg-transparent shadow-none"):
        for name, label, content in tabs:
            with ui.tab_panel(name).classes("px-0"):
                if content.strip():
                    _render_code_block(ui, content, code_classes)
                else:
                    ui.label(f"{label}尚未加载").classes("text-sm cb-muted")


def render_home_section(
    ui: UIFactoryLike,
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
        _render_page_intro(ui, HOME_PAGE.title, HOME_PAGE.description, "Console")
        with ui.card().classes("cb-card cb-hero w-full p-6"):
            with ui.row().classes("w-full items-start gap-6 flex-wrap lg:flex-nowrap"):
                with ui.column().classes("gap-3 grow"):
                    ui.label("控制台总览").classes("cb-kicker")
                    ui.label(model.home.badge_text).classes("text-3xl font-black tracking-tight")
                    ui.label(model.home.summary_text).classes("text-lg text-slate-800 font-semibold")
                    ui.label(model.home.primary_hint).classes("text-sm cb-muted max-w-2xl")
                    with ui.row().classes("gap-2 pt-2 flex-wrap"):
                        ui.button(model.home.primary_label, on_click=lambda: on_run_primary(model.home.primary_action)).props("color=primary unelevated")
                        ui.button("扫码登录微信", on_click=on_open_qr_login).props("outline")
                with ui.column().classes("gap-3 w-full lg:w-auto lg:min-w-[18rem] lg:max-w-[22rem]"):
                    ui.label("当前建议").classes("cb-kicker")
                    ui.label(f"{model.home.primary_label} / {model.home.primary_action}").classes("font-semibold text-slate-800")
                    _render_code_block(ui, model.home.quickstart_text)

        with _responsive_grid(ui, "grid-cols-1 xl:grid-cols-3"):
            with ui.card().classes("cb-card w-full p-5 xl:col-span-2"):
                _render_card_title(ui, "运行状态", "聚合后的服务视图和最常用控制入口。")
                status_panel_class, badge_class = _status_variant(model.home.badge_text)
                with ui.card().classes(f"cb-status-panel {status_panel_class} w-full mb-4 shadow-none"):
                    with ui.row().classes("w-full items-start justify-between gap-3 flex-wrap"):
                        with ui.column().classes("gap-2 grow"):
                            ui.label("系统状态").classes("cb-kicker")
                            ui.label(model.home.summary_text).classes("text-lg font-bold text-slate-900")
                            ui.label(model.home.primary_hint).classes("text-sm cb-muted max-w-2xl")
                        ui.label(model.home.badge_text).classes(f"{badge_class} self-start")
                with _responsive_grid(ui, "grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 mb-4"):
                    for label, value in (
                        ("状态标签", model.home.badge_text),
                        ("主动作", model.home.primary_label),
                        ("默认账号", model.active_account_id or "未设置"),
                        ("默认 Agent", model.bridge_agent_id or "main"),
                    ):
                        with ui.card().classes("cb-soft-card w-full p-4 shadow-none"):
                            ui.label(label).classes("cb-stat-label")
                            ui.label(value).classes("text-base font-bold text-slate-900 break-all")
                _render_code_block(ui, model.home.overview_text)
                with ui.row().classes("gap-2 pt-4 flex-wrap"):
                    ui.button("启动服务", on_click=lambda: on_run_action("start"))
                    ui.button("停止服务", on_click=lambda: on_run_action("stop"))
                    ui.button("重启服务", on_click=lambda: on_run_action("restart"))
                    ui.button("紧急停止", on_click=lambda: on_run_action("emergency-stop"), color="negative")

            with ui.card().classes("cb-card w-full p-5"):
                _render_card_title(ui, "工作入口", "任务投递、账号切换和通知配置。")
                with ui.column().classes("gap-3"):
                    with ui.card().classes("cb-soft-card w-full p-4 shadow-none"):
                        ui.label("当前账号").classes("cb-stat-label")
                        ui.label(model.active_account_id or "未选择").classes("text-base font-bold break-all")
                    with ui.card().classes("cb-soft-card w-full p-4 shadow-none"):
                        ui.label("通知状态").classes("cb-stat-label")
                        ui.label(
                            f"服务:{'开' if model.service_notice_enabled else '关'} / 配置:{'开' if model.config_notice_enabled else '关'} / 任务:{'开' if model.task_notice_enabled else '关'}"
                        ).classes("text-sm font-semibold")
                    ui.label(model.home.quickstart_status).classes("cb-chip cb-chip-warn w-fit")

        with _responsive_grid(ui, "grid-cols-1 xl:grid-cols-2"):
            with ui.card().classes("cb-card w-full p-5"):
                _render_card_title(ui, "提交任务", "像命令面板一样组织输入，减少表单感。")
                agent_options = {item.agent_id: item.label for item in model.agent_options}
                with ui.card().classes("cb-soft-card w-full p-4 shadow-none"):
                    ui.label("Command Composer").classes("cb-kicker")
                    prompt = ui.textarea(label="Prompt", placeholder="输入要发给 Agent 的内容").classes("w-full")
                    prompt.props("autogrow outlined input-class=text-base")
                    with _responsive_grid(ui, "grid-cols-1 md:grid-cols-3"):
                        agent = ui.select(
                            agent_options,
                            value=model.agent_options[0].agent_id if model.agent_options else "main",
                            label="Agent",
                        ).classes("w-full")
                        backend = ui.select(supported_backend_options(include_default=True), value="", label="后端").classes("w-full")
                        session_name = ui.input(label="会话名", placeholder="default").classes("w-full")
                    with ui.row().classes("gap-2 flex-wrap pt-3"):
                        for tip in ("保持 Prompt 简短", "必要时指定会话名", "后端为空表示默认路由"):
                            ui.label(tip).classes("cb-chip")
                ui.button(
                    "提交到 Hub",
                    on_click=lambda: on_submit_task(
                        agent.value or "main",
                        prompt.value or "",
                        session_name.value or "",
                        backend.value or "",
                    ),
                ).props("color=primary unelevated").classes("mt-4")

            with ui.card().classes("cb-card w-full p-5"):
                _render_card_title(ui, "账号管理", "运行时账号从配置中隔离，界面这里只负责切换。")
                ui.label(f"当前激活账号：{model.active_account_id}").classes("cb-chip w-fit")
                account_options = {item.account_id: item.label for item in model.account_options}
                account_select = ui.select(
                    account_options,
                    value=model.active_account_id or None,
                    label="切换账号",
                )
                with ui.row().classes("gap-2 flex-wrap"):
                    ui.button("切换当前账号", on_click=lambda: on_switch_account(account_select.value or ""))
                    ui.button("扫码登录微信", on_click=on_open_qr_login).props("outline")

            with ui.card().classes("cb-card w-full p-5"):
                _render_card_title(ui, "系统通知", "只保留必要控制，不在首页堆解释性文本。")
                service_notice = ui.switch("服务生命周期通知", value=model.service_notice_enabled)
                config_notice = ui.switch("配置变更通知", value=model.config_notice_enabled)
                task_notice = ui.switch("任务通知", value=model.task_notice_enabled)
                ui.label("服务、配置、任务通知都可独立控制，微信侧也支持 `/notify`。").classes("text-sm cb-muted")
                ui.button(
                    "应用通知设置",
                    on_click=lambda: on_set_weixin_notice_enabled(
                        bool(service_notice.value),
                        bool(config_notice.value),
                        bool(task_notice.value),
                    ),
                ).props("color=primary unelevated")


def render_issues_section(ui: UIFactoryLike, model: WebConsoleViewModel, on_run_repair_command) -> None:
    with ui.element("section").props(f"id={ISSUES_PAGE.anchor}").classes("w-full"):
        _render_page_intro(ui, ISSUES_PAGE.title, ISSUES_PAGE.description, "Health")
        with ui.card().classes("cb-card w-full p-5"):
            if model.checks_in_progress:
                chip_class, level_text = _severity_variant(model.checks_progress_text)
                with ui.row().classes("gap-2 items-center flex-wrap mb-3"):
                    ui.label(level_text).classes(chip_class)
                    ui.label(model.checks_progress_text).classes("text-sm text-amber-700 font-semibold")
            if model.issues:
                for item in model.issues:
                    chip_class, level_text = _severity_variant(f"{item.title} {item.detail}")
                    with ui.row().classes("gap-2 items-center flex-wrap"):
                        ui.label(level_text).classes(chip_class)
                        ui.label(item.title).classes("text-lg font-bold")
                    _render_code_block(ui, item.detail)
            else:
                ui.label("当前没有需要手动处理的异常。").classes("cb-muted")
            if model.repair_commands:
                ui.separator()
                ui.label("修复建议").classes("cb-section-title")
                for item in model.repair_commands:
                    with ui.card().classes("cb-soft-card w-full p-4 shadow-none"):
                        chip_class, level_text = _severity_variant(item.label)
                        with ui.row().classes("gap-2 items-center flex-wrap"):
                            ui.label(level_text).classes(chip_class)
                            ui.label(item.label).classes("font-semibold text-slate-900")
                        _render_code_block(ui, item.command)
                        if item.runnable:
                            ui.button("执行修复", on_click=lambda cmd=item.command, label=item.label: on_run_repair_command(cmd, label))
                        else:
                            ui.label("当前平台下这条修复建议需要手动执行。").classes("text-sm cb-muted")


def render_sessions_section(
    ui: UIFactoryLike,
    model: WebConsoleViewModel,
    on_select_session,
    on_set_session_page,
    on_load_session_detail,
    on_select_task,
    on_set_task_page,
    on_load_task_detail,
    on_set_task_filters,
    on_find_task_by_id,
    on_open_weixin_binding,
    on_open_weixin_binding_task,
    on_switch_weixin_binding_backend,
    on_reset_weixin_binding,
) -> None:
    with ui.element("section").props(f"id={SESSIONS_PAGE.anchor}").classes("w-full"):
        _render_page_intro(ui, SESSIONS_PAGE.title, SESSIONS_PAGE.description, "Sessions")
        with _responsive_grid(ui, "grid-cols-1 xl:grid-cols-2"):
            with ui.card().classes("cb-card w-full p-5"):
                _render_card_title(ui, "会话概览", "分页后的会话索引，优先用于定位和筛选。")
                session_options = {row.name: row.name for row in model.session_rows}
                ui.select(
                    session_options,
                    value=model.selected_session_name or None,
                    label="选择会话",
                    on_change=lambda event: on_select_session(event.value or ""),
                ).classes("w-full")
                with ui.row().classes("w-full gap-2 flex-wrap"):
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
                _render_session_summary_cards(ui, model, on_select_session)
                with ui.row().classes("w-full items-center justify-between gap-2 flex-wrap"):
                    ui.label(f"第 {model.session_page} / {model.session_total_pages} 页，共 {model.session_total_count} 条会话").classes("text-sm cb-muted")
                    with ui.row().classes("gap-2 flex-wrap"):
                        ui.button("上一页", on_click=lambda: on_set_session_page(model.session_page - 1)).props("outline").set_enabled(model.session_page > 1)
                        ui.button("下一页", on_click=lambda: on_set_session_page(model.session_page + 1)).props("outline").set_enabled(model.session_page < model.session_total_pages)
            with ui.card().classes("cb-card w-full p-5"):
                _render_card_title(ui, f"会话详情: {model.selected_session_name or '(未选择)'}", "保持按需加载，避免切页时读取大文本。")
                ui.button("加载会话详情", on_click=on_load_session_detail).props("outline")
                _render_detail_tabs(
                    ui,
                    [
                        ("session_detail", "会话详情", "\n".join(model.session_detail_lines)),
                        ("session_preview", "会话预览", "\n".join(model.session_conversation_lines)),
                    ],
                )

        with ui.card().classes("cb-card w-full p-5"):
            _render_card_title(ui, "最近任务", "筛选、分页和定位都保留，但视觉密度降下来。")
            if model.selected_session_name:
                ui.label(f"当前按会话过滤: {model.selected_session_name}").classes("text-sm cb-muted")
            if model.task_filtered_count != model.task_total_count:
                ui.label(f"当前显示 {model.task_filtered_count} / {model.task_total_count} 条任务").classes("text-sm cb-muted")
            with ui.row().classes("w-full gap-2 flex-wrap"):
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
            with ui.row().classes("w-full gap-2 flex-wrap"):
                task_lookup = ui.input(label="按 task_id 快速定位", placeholder="task-xxxxxxxxxx").classes("w-full sm:min-w-[18rem] sm:w-auto")
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
            _render_task_summary_cards(ui, model, on_select_task)
            with ui.row().classes("w-full items-center justify-between gap-2 flex-wrap"):
                ui.label(f"第 {model.task_page} / {model.task_total_pages} 页，共 {model.task_filtered_count or model.task_total_count} 条任务").classes("text-sm cb-muted")
                with ui.row().classes("gap-2 flex-wrap"):
                    ui.button("上一页", on_click=lambda: on_set_task_page(model.task_page - 1)).props("outline").set_enabled(model.task_page > 1)
                    ui.button("下一页", on_click=lambda: on_set_task_page(model.task_page + 1)).props("outline").set_enabled(model.task_page < model.task_total_pages)
            if not model.tasks:
                ui.label("当前筛选条件下没有任务。").classes("text-sm cb-muted")
            ui.separator()
            ui.label(f"任务详情: {model.selected_task_id or '(未选择)'}").classes("cb-section-title")
            ui.button("加载任务详情", on_click=on_load_task_detail).props("outline")
            _render_detail_tabs(
                ui,
                [
                    ("task_detail", "任务详情", "\n".join(model.task_detail_lines)),
                    ("task_output", "完整输出 / 错误", "\n".join(model.task_result_lines)),
                ],
                "max-h-80 overflow-auto",
            )
        with ui.card().classes("cb-card w-full p-5"):
            _render_card_title(ui, "微信会话绑定", "保留操作完整性，但把信息关系拆得更清楚。")
            if model.weixin_conversations:
                for item in model.weixin_conversations:
                    with ui.card().classes("cb-soft-card w-full p-4 shadow-none"):
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
                        with ui.row().classes("w-full items-center justify-between gap-3 flex-wrap"):
                            with ui.column().classes("gap-1"):
                                ui.label(f"发送方: {item.sender_id}").classes("font-semibold")
                                ui.label(f"Agent: {item.agent_id} | 当前会话: {item.current_session} | 当前后端: {item.current_backend}").classes("text-sm text-slate-700")
                                ui.label(f"会话数: {item.session_count} | 最近更新: {item.updated_at}").classes("text-sm cb-muted")
                                if item.latest_task_id:
                                    ui.label(f"最近任务: {item.latest_task_id} [{item.latest_task_status}]").classes("text-sm cb-muted")
                            with ui.column().classes("items-stretch lg:items-end gap-2 w-full lg:w-auto lg:min-w-[15rem]"):
                                backend_select = ui.select(
                                    supported_backend_options(),
                                    value=item.current_backend,
                                    label="当前会话后端",
                                ).classes("w-full")
                                with ui.row().classes("gap-2 flex-wrap"):
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
                ui.label("当前还没有微信会话绑定记录。").classes("cb-muted")
                ui.label("当 Bridge 收到消息后，这里会显示发送方当前使用的 Agent、会话和后端。").classes("text-sm cb-muted")


def render_diagnostics_section(
    ui: UIFactoryLike,
    model: WebConsoleViewModel,
    on_set_checks_page,
    on_switch_bridge_agent,
    on_set_agent_page,
    on_save_agent,
    on_delete_agent,
    on_terminate_external_agent,
    on_copy_external_session_hint,
) -> None:
    with ui.element("section").props(f"id={DIAGNOSTICS_PAGE.anchor}").classes("w-full"):
        _render_page_intro(ui, DIAGNOSTICS_PAGE.title, DIAGNOSTICS_PAGE.description, "Diagnostics")
        if model.checks_in_progress:
            chip_class, level_text = _severity_variant(model.checks_progress_text)
            with ui.row().classes("gap-2 items-center flex-wrap"):
                ui.label(level_text).classes(chip_class)
                ui.label(model.checks_progress_text).classes("text-sm text-amber-700 font-semibold")
        with _responsive_grid(ui, "grid-cols-1 xl:grid-cols-2"):
            with ui.card().classes("cb-card w-full p-5"):
                rows = [
                    {
                        "项目": check.label,
                        "状态": check.status_text,
                        "详情": check.detail,
                    }
                    for check in model.checks
                ]
                _render_card_title(ui, "环境检查", "步骤式检查会逐项回填结果，避免阻塞首屏。")
                ui.table(
                    columns=[{"name": key, "label": key, "field": key} for key in ["项目", "状态", "详情"]],
                    rows=rows,
                    row_key="项目",
                ).classes("w-full cb-table")
                with ui.row().classes("w-full items-center justify-between gap-2 flex-wrap"):
                    ui.label(f"第 {model.checks_page} / {model.checks_total_pages} 页，共 {model.checks_total_count} 项").classes("text-sm cb-muted")
                    with ui.row().classes("gap-2 flex-wrap"):
                        ui.button("上一页", on_click=lambda: on_set_checks_page(model.checks_page - 1)).props("outline").set_enabled(model.checks_page > 1)
                        ui.button("下一页", on_click=lambda: on_set_checks_page(model.checks_page + 1)).props("outline").set_enabled(model.checks_page < model.checks_total_pages)
            with ui.card().classes("cb-card w-full p-5"):
                _render_card_title(ui, "运行日志", "保留可读性，压低默认视觉噪声。")
                for title, content in model.log_sections:
                    ui.label(title).classes("font-semibold text-slate-700")
                    _render_code_block(ui, content, "max-h-60 overflow-auto")
                    ui.separator()
        with ui.card().classes("cb-card w-full p-5"):
            _render_card_title(ui, "Agent 管理", "配置、切换和运行态管理都收在一个模块里。")
            ui.label(f"微信桥当前默认 Agent：{model.bridge_agent_id or 'main'}").classes("text-sm cb-muted")
            bridge_agent_options = {item.agent_id: item.label for item in model.agent_options}
            bridge_agent_select = ui.select(
                bridge_agent_options,
                value=model.bridge_agent_id or (model.agent_options[0].agent_id if model.agent_options else "main"),
                label="微信桥默认 Agent",
            ).classes("w-full")
            with ui.row().classes("gap-2 flex-wrap"):
                ui.button("切换微信桥默认 Agent", on_click=lambda: on_switch_bridge_agent(bridge_agent_select.value or ""))
                ui.label("切换后会自动重启 Bridge 生效。").classes("text-sm cb-muted self-center")
            agent_rows = [
                {
                    "ID": item.agent_id,
                    "名称": item.name,
                    "后端": item.backend,
                    "启用": "是" if item.enabled else "否",
                    "状态": item.runtime_status,
                    "队列": item.queue_size,
                }
                for item in model.agent_entries
            ]
            ui.table(
                columns=[{"name": key, "label": key, "field": key} for key in ["ID", "名称", "后端", "启用", "状态", "队列"]],
                rows=agent_rows,
                row_key="ID",
            ).classes("w-full cb-table")
            with ui.row().classes("w-full items-center justify-between gap-2 flex-wrap"):
                ui.label(f"第 {model.agent_page} / {model.agent_total_pages} 页，共 {model.agent_total_count} 个 Agent").classes("text-sm cb-muted")
                with ui.row().classes("gap-2 flex-wrap"):
                    ui.button("上一页", on_click=lambda: on_set_agent_page(model.agent_page - 1)).props("outline").set_enabled(model.agent_page > 1)
                    ui.button("下一页", on_click=lambda: on_set_agent_page(model.agent_page + 1)).props("outline").set_enabled(model.agent_page < model.agent_total_pages)

            agent_lookup = {item.agent_id: item for item in model.agent_entries}
            agent_options = {"": "新建 Agent", **{item.agent_id: f"{item.name} ({item.agent_id})" for item in model.agent_entries}}
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

            with ui.row().classes("gap-2 flex-wrap"):
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
        with ui.card().classes("cb-card w-full p-5"):
            _render_card_title(ui, "外部终端 Agent 进程", "只显示未被 ChatBridge 接管的终端进程。")
            ui.label("当前支持明确区分和结束进程，不会把它们伪装成可接管会话。").classes("text-sm cb-muted")
            if model.external_agent_processes:
                for item in model.external_agent_processes:
                    with ui.card().classes("cb-soft-card w-full p-4 shadow-none"):
                        with ui.dialog() as terminate_dialog, ui.card().classes("min-w-[28rem]"):
                            ui.label("确认结束外部 Agent 进程").classes("text-lg font-semibold")
                            ui.label(f"PID {item.pid} 将被直接结束。这个操作只影响外部终端里手动启动的 Agent 进程。").classes("text-sm text-slate-600")
                            _render_code_block(ui, item.command_line)
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
                        _render_code_block(ui, item.command_line, "max-h-36 overflow-auto")
                        with ui.row().classes("gap-2 flex-wrap"):
                            if item.session_hint:
                                ui.button(
                                    "复制会话标识",
                                    on_click=lambda session_hint=item.session_hint: on_copy_external_session_hint(session_hint),
                                ).props("outline")
                            ui.button("结束进程", color="negative", on_click=terminate_dialog.open).props("outline")
            else:
                ui.label("当前没有发现外部终端里手动启动的 Agent 进程。").classes("cb-muted")
