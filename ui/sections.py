from __future__ import annotations

from typing import Callable, Protocol

try:
    from typing import Self
except ImportError:  # Python 3.10 compatibility
    from typing_extensions import Self

from agent_backends import supported_backend_options
from core.navigation import DIAGNOSTICS_PAGE, HOME_PAGE, SESSIONS_PAGE
from core.view_models import WebConsoleViewModel


Translator = Callable[..., str]


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


def _tr(t: Translator, key: str, fallback: str, **kwargs: object) -> str:
    value = t(key, **kwargs)
    return value if value != key else fallback.format(**kwargs)


def _render_page_intro(ui: UIFactoryLike, title: str, description: str, kicker: str) -> None:
    with ui.column().classes("gap-1 mb-1"):
        ui.label(kicker).classes("cb-kicker")
        ui.label(title).classes("cb-page-title text-slate-900")
        ui.label(description).classes("text-sm cb-muted max-w-3xl")


def _render_card_title(ui: UIFactoryLike, title: str, detail: str = "") -> None:
    with ui.column().classes("gap-1 mb-3"):
        ui.label(title).classes("cb-section-title")
        if detail:
            ui.label(detail).classes("text-sm cb-muted")


def _render_code_block(ui: UIFactoryLike, content: str, extra_classes: str = "", empty_text: str = "暂无数据") -> None:
    ui.code(content or empty_text).classes(f"cb-code w-full {extra_classes}".strip())


def _responsive_grid(ui: UIFactoryLike, classes: str) -> UIElementLike:
    return ui.element("div").classes(f"grid w-full gap-4 {classes}".strip())


def _panel(ui: UIFactoryLike, classes: str = "") -> UIElementLike:
    return ui.element("div").classes(f"cb-panel w-full p-4 {classes}".strip())


def _render_disclosure_code(ui: UIFactoryLike, title: str, content: str) -> None:
    with ui.element("details").classes("cb-disclosure cb-panel w-full p-4"):
        with ui.element("summary").classes("flex items-center justify-between gap-3 font-semibold text-slate-900"):
            ui.label(title)
        _render_code_block(ui, content, "mt-3 max-h-96 overflow-auto")


def _render_meta_line(ui: UIFactoryLike, text: str) -> None:
    ui.label(text).classes("text-sm cb-muted")


def _status_variant(text: str) -> tuple[str, str]:
    lowered = text.lower()
    if "部分" in text or "partial" in lowered or "not ready" in lowered:
        return "cb-status-partial", "cb-chip cb-chip-warn"
    if "运行" in text or "running" in lowered or "ready" in lowered:
        return "cb-status-running", "cb-chip cb-chip-ok"
    return "cb-status-stopped", "cb-chip cb-chip-danger"


def _task_status_filter_options(t: Translator, options: list[str]) -> dict[str, str]:
    return {
        "": _tr(t, "ui.web.filter.all_status", "全部状态"),
        **{item: _tr(t, f"bridge.task.status.{item}", item) for item in options},
    }


def _severity_variant(text: str, t: Translator) -> tuple[str, str]:
    lowered = text.lower()
    if any(keyword in lowered for keyword in ("失败", "缺失", "错误", "未就绪", "异常", "failed", "missing", "error", "not ready")):
        return "cb-chip cb-chip-danger", _tr(t, "ui.web.severity.high", "高风险")
    if any(keyword in lowered for keyword in ("等待", "部分", "建议", "手动", "进行中", "waiting", "partial", "manual", "running", "recommended")):
        return "cb-chip cb-chip-warn", _tr(t, "ui.web.severity.attention", "需关注")
    return "cb-chip cb-chip-ok", _tr(t, "ui.web.severity.normal", "正常")


def _render_pagination(
    ui: UIFactoryLike,
    t: Translator,
    page: int,
    total_pages: int,
    count: int,
    unit_key: str,
    unit_fallback: str,
    on_prev,
    on_next,
) -> None:
    with ui.row().classes("w-full items-center justify-between gap-2 flex-wrap"):
        ui.label(
            _tr(
                t,
                "ui.web.pagination",
                "第 {page} / {total_pages} 页，共 {count} {unit}",
                page=page,
                total_pages=total_pages,
                count=count,
                unit=_tr(t, unit_key, unit_fallback),
            )
        ).classes("text-sm cb-muted")
        with ui.row().classes("gap-2 flex-wrap"):
            ui.button(_tr(t, "ui.web.pagination.prev", "上一页"), on_click=on_prev, icon="chevron_left").props("outline").set_enabled(page > 1)
            ui.button(_tr(t, "ui.web.pagination.next", "下一页"), on_click=on_next, icon="chevron_right").props("outline").set_enabled(page < total_pages)


def _render_session_summary_cards(ui: UIFactoryLike, model: WebConsoleViewModel, t: Translator, on_select_session) -> None:
    with _responsive_grid(ui, "grid-cols-1 lg:grid-cols-2"):
        for row in model.session_rows:
            selected = row.name == model.selected_session_name
            panel_classes = "border-2 border-[var(--cb-accent)]" if selected else ""
            with _panel(ui, panel_classes):
                with ui.row().classes("w-full items-start justify-between gap-3 flex-wrap"):
                    with ui.column().classes("gap-1 grow"):
                        ui.label(row.name).classes("text-lg font-bold text-slate-900 break-all")
                        _render_meta_line(ui, _tr(t, "ui.web.meta.status", "状态: {value}", value=row.status))
                    ui.button(
                        _tr(t, "ui.web.action.view_session", "查看会话"),
                        on_click=lambda session_name=row.name: on_select_session(session_name),
                        icon="open_in_new",
                    ).props("color=primary unelevated" if selected else "outline")
                with ui.row().classes("gap-2 flex-wrap pt-2"):
                    for label, value in (
                        (_tr(t, "ui.table.queue", "队列"), row.queue_size),
                        (_tr(t, "ui.table.success", "成功"), row.success_count),
                        (_tr(t, "ui.table.failure", "失败"), row.failure_count),
                    ):
                        with ui.element("div").classes("border border-[var(--cb-border)] rounded-[6px] bg-white w-auto min-w-[5.5rem] px-3 py-2"):
                            ui.label(label).classes("cb-stat-label")
                            ui.label(str(value)).classes("text-base font-bold text-slate-900")


def _render_task_summary_cards(ui: UIFactoryLike, model: WebConsoleViewModel, t: Translator, on_select_task) -> None:
    with ui.column().classes("w-full gap-3"):
        for task in model.tasks:
            with _panel(ui):
                with ui.row().classes("w-full items-start justify-between gap-3 flex-wrap"):
                    with ui.column().classes("gap-1 grow"):
                        ui.label(f"{task.agent_name} / {task.status}").classes("text-base font-bold text-slate-900")
                        _render_meta_line(
                            ui,
                            _tr(
                                t,
                                "ui.web.task.meta",
                                "{created_at} | 后端: {backend} | 会话: {session}",
                                created_at=task.created_at,
                                backend=task.backend,
                                session=task.session_name or _tr(t, "ui.web.value.uncategorized", "(未归类)"),
                            ),
                        )
                        ui.label(task.prompt_summary).classes("text-sm text-slate-800")
                        ui.label(task.result_summary).classes("text-sm cb-muted")
                    ui.button(
                        _tr(t, "ui.web.action.view_task", "查看任务"),
                        on_click=lambda task_id=task.task_id, session_name=task.session_name: on_select_task(task_id, session_name),
                        icon="receipt_long",
                    ).props("color=primary unelevated" if task.task_id == model.selected_task_id else "outline")


def _render_detail_tabs(ui: UIFactoryLike, t: Translator, tabs: list[tuple[str, str, str]], code_classes: str = "") -> None:
    with ui.tabs().classes("w-full") as tab_bar:
        for name, label, _content in tabs:
            ui.tab(name, label=label)
    initial_tab = tabs[0][0]
    tab_bar.set_value(initial_tab)
    with ui.tab_panels(tab_bar, value=initial_tab).classes("w-full bg-transparent shadow-none"):
        for name, label, content in tabs:
            with ui.tab_panel(name).classes("px-0"):
                if content.strip():
                    _render_code_block(ui, content, code_classes)
                else:
                    ui.label(_tr(t, "ui.web.detail.not_loaded", "{label}尚未加载", label=label)).classes("text-sm cb-muted")


def render_home_section(
    ui: UIFactoryLike,
    model: WebConsoleViewModel,
    t: Translator,
    on_run_action,
    on_submit_task,
    on_switch_account,
    on_set_weixin_notice_enabled,
    on_open_qr_login,
) -> None:
    with ui.element("section").props(f"id={HOME_PAGE.anchor}").classes("w-full"):
        _render_page_intro(ui, _tr(t, "ui.tab.home", HOME_PAGE.title), _tr(t, "ui.page.home.description", HOME_PAGE.description), "Console")
        with _responsive_grid(ui, "grid-cols-1"):
            with ui.card().classes("cb-card w-full p-5"):
                _render_card_title(ui, _tr(t, "ui.web.home.service_controls", "服务控制"))
                status_panel_class, badge_class = _status_variant(f"{model.home.badge_text} {model.home.summary_text}")
                with ui.element("div").classes(f"cb-status-panel {status_panel_class} w-full mb-4"):
                    with ui.row().classes("w-full items-start justify-between gap-3 flex-wrap"):
                        with ui.column().classes("gap-2 grow"):
                            ui.label(_tr(t, "ui.web.home.system_status", "系统状态")).classes("cb-kicker")
                            ui.label(model.home.summary_text).classes("text-base font-bold text-slate-900")
                            ui.label(model.home.primary_hint).classes("text-sm cb-muted")
                        ui.label(model.home.badge_text).classes(f"{badge_class} self-start")
                with ui.row().classes("gap-2 pt-4 flex-wrap"):
                    ui.button(_tr(t, "ui.primary.start.label", "启动服务"), on_click=lambda: on_run_action("start"), icon="play_arrow")
                    ui.button(_tr(t, "ui.primary.stop.label", "停止服务"), on_click=lambda: on_run_action("stop"), icon="stop")
                    ui.button(_tr(t, "ui.web.action.restart", "重启服务"), on_click=lambda: on_run_action("restart"), icon="restart_alt")
                    ui.button(_tr(t, "ui.web.action.emergency_stop", "紧急停止"), on_click=lambda: on_run_action("emergency-stop"), color="negative", icon="warning")

        with _responsive_grid(ui, "grid-cols-1 xl:grid-cols-2"):
            with ui.card().classes("cb-card w-full p-5"):
                _render_card_title(ui, _tr(t, "ui.web.home.submit_task", "提交任务"))
                agent_options = {item.agent_id: item.label for item in model.agent_options}
                with _panel(ui):
                    prompt = ui.textarea(label=_tr(t, "ui.web.field.prompt", "Prompt"), placeholder=_tr(t, "ui.web.form.prompt_placeholder", "输入要发给 Agent 的内容")).classes("w-full")
                    prompt.props("autogrow outlined input-class=text-base")
                    with _responsive_grid(ui, "grid-cols-1 md:grid-cols-3"):
                        agent = ui.select(
                            agent_options,
                            value=model.agent_options[0].agent_id if model.agent_options else "main",
                            label=_tr(t, "ui.web.field.agent", "Agent"),
                        ).classes("w-full")
                        backend = ui.select(supported_backend_options(include_default=True), value="", label=_tr(t, "ui.web.field.backend", "后端")).classes("w-full")
                        session_name = ui.input(label=_tr(t, "ui.web.field.session_name", "会话名"), placeholder="default").classes("w-full")
                    with ui.row().classes("gap-2 flex-wrap pt-3"):
                        for tip in (
                            _tr(t, "ui.web.tip.short_prompt", "保持 Prompt 简短"),
                            _tr(t, "ui.web.tip.session_name", "必要时指定会话名"),
                            _tr(t, "ui.web.tip.default_backend", "后端为空表示默认路由"),
                        ):
                            ui.label(tip).classes("cb-chip")
                ui.button(
                    _tr(t, "ui.web.action.submit_to_hub", "提交到 Hub"),
                    on_click=lambda: on_submit_task(
                        agent.value or "main",
                        prompt.value or "",
                        session_name.value or "",
                        backend.value or "",
                    ),
                    icon="send",
                ).props("color=primary unelevated").classes("mt-4")

            with ui.card().classes("cb-card w-full p-5"):
                _render_card_title(ui, _tr(t, "ui.web.home.accounts", "账号管理"))
                ui.label(_tr(t, "ui.web.account.active", "当前激活账号：{account}", account=model.active_account_label)).classes("cb-chip w-fit")
                account_options = {item.account_id: item.label for item in model.account_options}
                account_select = ui.select(
                    account_options,
                    value=model.active_account_id or None,
                    label=_tr(t, "ui.web.field.switch_account", "切换账号"),
                )
                with ui.row().classes("gap-2 flex-wrap"):
                    ui.button(_tr(t, "ui.web.action.switch_account", "切换当前账号"), on_click=lambda: on_switch_account(account_select.value or ""), icon="swap_horiz")
                    ui.button(_tr(t, "ui.button.login", "扫码登录微信"), on_click=on_open_qr_login, icon="qr_code_scanner").props("outline")

            with ui.card().classes("cb-card w-full p-5"):
                _render_card_title(ui, _tr(t, "ui.web.home.notifications", "系统通知"))
                service_notice = ui.switch(_tr(t, "ui.web.notice.service", "服务生命周期通知"), value=model.service_notice_enabled)
                config_notice = ui.switch(_tr(t, "ui.web.notice.config", "配置变更通知"), value=model.config_notice_enabled)
                task_notice = ui.switch(_tr(t, "ui.web.notice.task", "任务通知"), value=model.task_notice_enabled)
                ui.button(
                    _tr(t, "ui.web.action.apply_notice", "应用通知设置"),
                    on_click=lambda: on_set_weixin_notice_enabled(
                        bool(service_notice.value),
                        bool(config_notice.value),
                        bool(task_notice.value),
                    ),
                    icon="notifications_active",
                ).props("color=primary unelevated")


def _render_repair_suggestions(ui: UIFactoryLike, model: WebConsoleViewModel, t: Translator, on_run_repair_command) -> None:
    if not model.repair_commands:
        return
    with ui.card().classes("cb-card w-full p-5"):
        _render_card_title(ui, _tr(t, "ui.web.repairs.title", "修复建议"))
        for item in model.repair_commands:
            with _panel(ui):
                chip_class, level_text = _severity_variant(item.label, t)
                with ui.row().classes("gap-2 items-center flex-wrap"):
                    ui.label(level_text).classes(chip_class)
                    ui.label(item.label).classes("font-semibold text-slate-900")
                _render_code_block(ui, item.command)
                if item.runnable:
                    ui.button(_tr(t, "ui.web.action.run_repair", "执行修复"), on_click=lambda cmd=item.command, label=item.label: on_run_repair_command(cmd, label), icon="build")
                else:
                    ui.label(_tr(t, "ui.web.repairs.manual", "当前平台下这条修复建议需要手动执行。")).classes("text-sm cb-muted")


def render_sessions_section(
    ui: UIFactoryLike,
    model: WebConsoleViewModel,
    t: Translator,
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
        _render_page_intro(ui, _tr(t, "ui.tab.sessions", SESSIONS_PAGE.title), _tr(t, "ui.page.sessions.description", SESSIONS_PAGE.description), "Sessions")
        with _responsive_grid(ui, "grid-cols-1 xl:grid-cols-2"):
            with ui.card().classes("cb-card w-full p-5"):
                _render_card_title(ui, _tr(t, "ui.web.sessions.overview", "会话概览"))
                if model.session_rows:
                    _render_session_summary_cards(ui, model, t, on_select_session)
                else:
                    ui.label(_tr(t, "ui.web.sessions.empty", "当前没有会话记录。")).classes("text-sm cb-muted")
                _render_pagination(
                    ui,
                    t,
                    model.session_page,
                    model.session_total_pages,
                    model.session_total_count,
                    "ui.web.unit.session",
                    "条会话",
                    lambda: on_set_session_page(model.session_page - 1),
                    lambda: on_set_session_page(model.session_page + 1),
                )
            with ui.card().classes("cb-card w-full p-5"):
                _render_card_title(ui, _tr(t, "ui.web.sessions.detail_title", "会话详情: {session}", session=model.selected_session_name or _tr(t, "ui.web.value.unselected", "(未选择)")))
                ui.button(_tr(t, "ui.web.action.load_session_detail", "加载会话详情"), on_click=on_load_session_detail, icon="download").props("outline")
                _render_detail_tabs(
                    ui,
                    t,
                    [
                        ("session_detail", _tr(t, "ui.web.tab.session_detail", "会话详情"), "\n".join(model.session_detail_lines)),
                        ("session_preview", _tr(t, "ui.web.tab.session_preview", "会话预览"), "\n".join(model.session_conversation_lines)),
                    ],
                )

        with ui.card().classes("cb-card w-full p-5"):
            _render_card_title(ui, _tr(t, "ui.web.tasks.recent", "最近任务"))
            if model.selected_session_name:
                ui.label(_tr(t, "ui.web.tasks.filtered_by_session", "当前按会话过滤: {session}", session=model.selected_session_name)).classes("text-sm cb-muted")
            if model.task_filtered_count != model.task_total_count:
                ui.label(_tr(t, "ui.web.tasks.filtered_count", "当前显示 {filtered} / {total} 条任务", filtered=model.task_filtered_count, total=model.task_total_count)).classes("text-sm cb-muted")
            with ui.row().classes("w-full gap-2 flex-wrap"):
                status_filter = ui.select(
                    _task_status_filter_options(t, model.task_status_options),
                    value=model.selected_task_status,
                    label=_tr(t, "ui.table.status", "状态"),
                ).classes("min-w-[12rem]")
                agent_filter = ui.select(
                    {"": _tr(t, "ui.web.filter.all_agent", "全部 Agent"), **{item: item for item in model.task_agent_options}},
                    value=model.selected_task_agent,
                    label=_tr(t, "ui.web.field.agent", "Agent"),
                ).classes("min-w-[14rem]")
                backend_filter = ui.select(
                    {"": _tr(t, "ui.web.filter.all_backend", "全部后端"), **{item: item for item in model.task_backend_options}},
                    value=model.selected_task_backend,
                    label=_tr(t, "ui.web.field.backend", "后端"),
                ).classes("min-w-[12rem]")
                ui.button(
                    _tr(t, "ui.web.action.apply_filter", "应用筛选"),
                    on_click=lambda: on_set_task_filters(
                        status_filter.value or "",
                        agent_filter.value or "",
                        backend_filter.value or "",
                    ),
                    icon="filter_alt",
                ).props("outline")
                ui.button(_tr(t, "ui.web.action.clear_filter", "清空筛选"), on_click=lambda: on_set_task_filters("", "", ""), icon="filter_alt_off").props("flat")
            with ui.row().classes("w-full gap-2 flex-wrap"):
                task_lookup = ui.input(label=_tr(t, "ui.web.field.lookup_task", "按 task_id 快速定位"), placeholder="task-xxxxxxxxxx").classes("w-full sm:min-w-[18rem] sm:w-auto")
                ui.button(_tr(t, "ui.web.action.locate_task", "定位任务"), on_click=lambda: on_find_task_by_id(task_lookup.value or ""), icon="search").props("outline")
            _render_task_summary_cards(ui, model, t, on_select_task)
            _render_pagination(
                ui,
                t,
                model.task_page,
                model.task_total_pages,
                model.task_filtered_count or model.task_total_count,
                "ui.web.unit.task",
                "条任务",
                lambda: on_set_task_page(model.task_page - 1),
                lambda: on_set_task_page(model.task_page + 1),
            )
            if not model.tasks:
                ui.label(_tr(t, "ui.web.tasks.empty", "当前筛选条件下没有任务。")).classes("text-sm cb-muted")
            ui.separator()
            ui.label(_tr(t, "ui.web.tasks.detail_title", "任务详情: {task}", task=model.selected_task_id or _tr(t, "ui.web.value.unselected", "(未选择)"))).classes("cb-section-title")
            ui.button(_tr(t, "ui.web.action.load_task_detail", "加载任务详情"), on_click=on_load_task_detail, icon="download").props("outline")
            _render_detail_tabs(
                ui,
                t,
                [
                    ("task_detail", _tr(t, "ui.web.tab.task_detail", "任务详情"), "\n".join(model.task_detail_lines)),
                    ("task_output", _tr(t, "ui.web.tab.task_output", "完整输出 / 错误"), "\n".join(model.task_result_lines)),
                ],
                "max-h-80 overflow-auto",
            )
        with ui.card().classes("cb-card w-full p-5"):
            _render_card_title(ui, _tr(t, "ui.web.bindings.title", "微信会话绑定"))
            if model.weixin_conversations:
                for item in model.weixin_conversations:
                    with _panel(ui):
                        with ui.dialog() as reset_dialog, ui.card().classes("min-w-[28rem]"):
                            ui.label(_tr(t, "ui.web.bindings.reset_title", "确认重置微信会话")).classes("text-lg font-semibold")
                            ui.label(_tr(t, "ui.web.bindings.reset_body", "这会删除该发送方的会话状态，并在 Bridge 运行中时自动重启使其生效。")).classes("text-sm text-slate-600")
                            with ui.row().classes("justify-end gap-2 w-full"):
                                ui.button(_tr(t, "ui.button.cancel", "取消"), on_click=reset_dialog.close).props("flat")
                                ui.button(
                                    _tr(t, "ui.web.action.confirm_reset", "确认重置"),
                                    color="negative",
                                    on_click=lambda sender_id=item.sender_id: (
                                        on_reset_weixin_binding(sender_id),
                                        reset_dialog.close(),
                                    ),
                                )
                        with ui.row().classes("w-full items-center justify-between gap-3 flex-wrap"):
                            with ui.column().classes("gap-1"):
                                ui.label(_tr(t, "ui.web.bindings.sender", "发送方: {sender}", sender=item.sender_id)).classes("font-semibold")
                                ui.label(_tr(t, "ui.web.bindings.current", "Agent: {agent} | 当前会话: {session} | 当前后端: {backend}", agent=item.agent_id, session=item.current_session, backend=item.current_backend)).classes("text-sm text-slate-700")
                                ui.label(_tr(t, "ui.web.bindings.count", "会话数: {count} | 最近更新: {updated_at}", count=item.session_count, updated_at=item.updated_at)).classes("text-sm cb-muted")
                                if item.latest_task_id:
                                    ui.label(_tr(t, "ui.web.bindings.latest_task", "最近任务: {task} [{status}]", task=item.latest_task_id, status=item.latest_task_status)).classes("text-sm cb-muted")
                            with ui.column().classes("items-stretch lg:items-end gap-2 w-full lg:w-auto lg:min-w-[15rem]"):
                                backend_select = ui.select(
                                    supported_backend_options(),
                                    value=item.current_backend,
                                    label=_tr(t, "ui.web.field.current_backend", "当前会话后端"),
                                ).classes("w-full")
                                with ui.row().classes("gap-2 flex-wrap"):
                                    ui.button(
                                        _tr(t, "ui.web.action.open_session", "打开该会话"),
                                        on_click=lambda session_name=item.current_session: on_open_weixin_binding(session_name),
                                        icon="forum",
                                    ).props("outline")
                                    ui.button(
                                        _tr(t, "ui.web.action.open_latest_task", "打开最近任务"),
                                        on_click=lambda task_id=item.latest_task_id, session_name=item.latest_task_session: on_open_weixin_binding_task(task_id, session_name),
                                        icon="receipt_long",
                                    ).props("outline")
                                    ui.button(
                                        _tr(t, "ui.web.action.switch_backend", "切换后端"),
                                        on_click=lambda sender_id=item.sender_id, select=backend_select: on_switch_weixin_binding_backend(sender_id, select.value or ""),
                                        icon="swap_horiz",
                                    )
                                    ui.button(_tr(t, "ui.web.action.reset_session", "重置会话"), color="negative", on_click=reset_dialog.open, icon="restart_alt").props("outline")
            else:
                ui.label(_tr(t, "ui.web.bindings.empty", "当前还没有微信会话绑定记录。")).classes("cb-muted")
                ui.label(_tr(t, "ui.web.bindings.empty_detail", "当 Bridge 收到消息后，这里会显示发送方当前使用的 Agent、会话和后端。")).classes("text-sm cb-muted")


def render_diagnostics_section(
    ui: UIFactoryLike,
    model: WebConsoleViewModel,
    t: Translator,
    on_set_checks_page,
    on_switch_bridge_agent,
    on_set_agent_page,
    on_save_agent,
    on_delete_agent,
    on_terminate_external_agent,
    on_copy_external_session_hint,
    on_run_repair_command,
) -> None:
    with ui.element("section").props(f"id={DIAGNOSTICS_PAGE.anchor}").classes("w-full"):
        _render_page_intro(ui, _tr(t, "ui.tab.logs", DIAGNOSTICS_PAGE.title), _tr(t, "ui.page.logs.description", DIAGNOSTICS_PAGE.description), "Diagnostics")
        if model.checks_in_progress:
            chip_class, level_text = _severity_variant(model.checks_progress_text, t)
            with ui.row().classes("gap-2 items-center flex-wrap"):
                ui.label(level_text).classes(chip_class)
                ui.label(model.checks_progress_text).classes("text-sm text-amber-700 font-semibold")
        with _responsive_grid(ui, "grid-cols-1 xl:grid-cols-2"):
            with ui.card().classes("cb-card w-full p-5"):
                column_project = _tr(t, "ui.web.table.project", "项目")
                column_status = _tr(t, "ui.table.status", "状态")
                column_detail = _tr(t, "ui.web.table.detail", "详情")
                rows = [
                    {
                        column_project: check.label,
                        column_status: check.status_text,
                        column_detail: check.detail,
                    }
                    for check in model.checks
                ]
                _render_card_title(ui, _tr(t, "ui.web.diagnostics.checks", "环境检查"))
                ui.table(
                    columns=[{"name": key, "label": key, "field": key} for key in [column_project, column_status, column_detail]],
                    rows=rows,
                    row_key=column_project,
                ).classes("w-full cb-table")
                _render_pagination(
                    ui,
                    t,
                    model.checks_page,
                    model.checks_total_pages,
                    model.checks_total_count,
                    "ui.web.unit.check",
                    "项",
                    lambda: on_set_checks_page(model.checks_page - 1),
                    lambda: on_set_checks_page(model.checks_page + 1),
                )
            with ui.card().classes("cb-card w-full p-5"):
                _render_card_title(ui, _tr(t, "ui.card.activity", "运行日志"))
                for title, content in model.log_sections:
                    ui.label(title).classes("font-semibold text-slate-700")
                    _render_code_block(ui, content, "max-h-60 overflow-auto")
                    ui.separator()
        _render_repair_suggestions(ui, model, t, on_run_repair_command)
        with ui.card().classes("cb-card w-full p-5"):
            _render_card_title(ui, _tr(t, "ui.web.agents.title", "Agent 管理"))
            ui.label(_tr(t, "ui.web.agents.default", "微信桥当前默认 Agent：{agent}", agent=model.bridge_agent_id or "main")).classes("text-sm cb-muted")
            bridge_agent_options = {item.agent_id: item.label for item in model.agent_options}
            bridge_agent_select = ui.select(
                bridge_agent_options,
                value=model.bridge_agent_id or (model.agent_options[0].agent_id if model.agent_options else "main"),
                label=_tr(t, "ui.web.field.default_agent", "微信桥默认 Agent"),
            ).classes("w-full")
            with ui.row().classes("gap-2 flex-wrap"):
                ui.button(_tr(t, "ui.web.action.switch_default_agent", "切换微信桥默认 Agent"), on_click=lambda: on_switch_bridge_agent(bridge_agent_select.value or ""), icon="swap_horiz")
                ui.label(_tr(t, "ui.web.agents.restart_hint", "切换后会自动重启 Bridge 生效。")).classes("text-sm cb-muted self-center")
            column_name = _tr(t, "ui.web.table.name", "名称")
            column_backend = _tr(t, "ui.web.field.backend", "后端")
            column_enabled = _tr(t, "ui.web.table.enabled", "启用")
            column_queue = _tr(t, "ui.table.queue", "队列")
            agent_rows = [
                {
                    "ID": item.agent_id,
                    column_name: item.name,
                    column_backend: item.backend,
                    column_enabled: _tr(t, "ui.web.value.yes", "是") if item.enabled else _tr(t, "ui.web.value.no", "否"),
                    column_status: item.runtime_status,
                    column_queue: item.queue_size,
                }
                for item in model.agent_entries
            ]
            ui.table(
                columns=[{"name": key, "label": key, "field": key} for key in ["ID", column_name, column_backend, column_enabled, column_status, column_queue]],
                rows=agent_rows,
                row_key="ID",
            ).classes("w-full cb-table")
            _render_pagination(
                ui,
                t,
                model.agent_page,
                model.agent_total_pages,
                model.agent_total_count,
                "ui.web.unit.agent",
                "个 Agent",
                lambda: on_set_agent_page(model.agent_page - 1),
                lambda: on_set_agent_page(model.agent_page + 1),
            )

            with _panel(ui, "mt-3"):
                agent_lookup = {item.agent_id: item for item in model.agent_entries}
                agent_options = {"": _tr(t, "ui.web.agents.new", "新建 Agent"), **{item.agent_id: f"{item.name} ({item.agent_id})" for item in model.agent_entries}}
                selected_agent = ui.select(agent_options, value="", label=_tr(t, "ui.web.field.edit_agent", "编辑 Agent")).classes("w-full")
                with _responsive_grid(ui, "grid-cols-1 md:grid-cols-2"):
                    agent_id = ui.input(label="Agent ID", placeholder="assistant-1").classes("w-full")
                    agent_name = ui.input(label=column_name, placeholder=_tr(t, "ui.web.placeholder.agent_name", "客服助手")).classes("w-full")
                    workdir = ui.input(label=_tr(t, "ui.web.field.workdir", "工作目录"), placeholder="workspace").classes("w-full")
                    session_file = ui.input(label=_tr(t, "ui.web.field.session_file", "会话文件"), placeholder="sessions/assistant-1.txt").classes("w-full")
                    backend = ui.select(supported_backend_options(), value="codex", label=column_backend).classes("w-full")
                    model_input = ui.input(label=_tr(t, "ui.web.field.model", "模型"), placeholder=_tr(t, "ui.web.value.optional", "可选")).classes("w-full")
                prompt_prefix = ui.textarea(label=_tr(t, "ui.web.field.prompt_prefix", "Prompt Prefix"), placeholder=_tr(t, "ui.web.value.optional", "可选")).classes("w-full")
                enabled = ui.switch(column_enabled, value=True)

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
                ui.label(_tr(t, "ui.web.agents.delete_title", "确认删除 Agent")).classes("text-lg font-semibold")
                ui.label(_tr(t, "ui.web.agents.delete_body", "删除后会移除该 Agent 配置和任务记录。若该 Agent 正被微信桥使用，Hub 会拒绝删除。")).classes("text-sm text-slate-600")
                with ui.row().classes("justify-end gap-2 w-full"):
                    ui.button(_tr(t, "ui.button.cancel", "取消"), on_click=delete_dialog.close).props("flat")
                    ui.button(
                        _tr(t, "ui.web.action.confirm_delete", "确认删除"),
                        color="negative",
                        on_click=lambda: (
                            on_delete_agent(selected_agent.value or ""),
                            delete_dialog.close(),
                        ),
                    )

            with ui.row().classes("gap-2 flex-wrap"):
                ui.button(
                    _tr(t, "ui.web.action.save_agent", "保存 Agent"),
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
                    icon="save",
                )
                ui.button(_tr(t, "ui.web.action.reset_form", "重置表单"), on_click=lambda: fill_agent_form(selected_agent.value or ""), icon="restart_alt").props("outline")
                ui.button(
                    _tr(t, "ui.web.action.delete_agent", "删除 Agent"),
                    color="negative",
                    on_click=lambda: delete_dialog.open() if selected_agent.value else None,
                    icon="delete",
                ).props("outline")
        with ui.card().classes("cb-card w-full p-5"):
            _render_card_title(ui, _tr(t, "ui.web.external.title", "外部终端 Agent 进程"))
            if model.external_agent_processes:
                for item in model.external_agent_processes:
                    with _panel(ui):
                        with ui.dialog() as terminate_dialog, ui.card().classes("min-w-[28rem]"):
                            ui.label(_tr(t, "ui.web.external.terminate_title", "确认结束外部 Agent 进程")).classes("text-lg font-semibold")
                            ui.label(_tr(t, "ui.web.external.terminate_body", "PID {pid} 将被直接结束。这个操作只影响外部终端里手动启动的 Agent 进程。", pid=item.pid)).classes("text-sm text-slate-600")
                            _render_code_block(ui, item.command_line)
                            with ui.row().classes("justify-end gap-2 w-full"):
                                ui.button(_tr(t, "ui.button.cancel", "取消"), on_click=terminate_dialog.close).props("flat")
                                ui.button(
                                    _tr(t, "ui.web.action.confirm_terminate", "确认结束"),
                                    color="negative",
                                    on_click=lambda pid=item.pid: (
                                        on_terminate_external_agent(pid),
                                        terminate_dialog.close(),
                                    ),
                                )
                        ui.label(f"PID {item.pid} | {item.backend} | {item.managed_label}").classes("font-semibold")
                        ui.label(_tr(t, "ui.web.external.process_name", "进程名: {name}", name=item.name)).classes("text-sm text-slate-700")
                        if item.session_hint:
                            ui.label(_tr(t, "ui.web.external.session_hint", "会话标识: {hint}", hint=item.session_hint)).classes("text-sm text-slate-700")
                        _render_code_block(ui, item.command_line, "max-h-36 overflow-auto")
                        with ui.row().classes("gap-2 flex-wrap"):
                            if item.session_hint:
                                ui.button(
                                    _tr(t, "ui.web.action.copy_session_hint", "复制会话标识"),
                                    on_click=lambda session_hint=item.session_hint: on_copy_external_session_hint(session_hint),
                                    icon="content_copy",
                                ).props("outline")
                            ui.button(_tr(t, "ui.web.action.terminate_process", "结束进程"), color="negative", on_click=terminate_dialog.open, icon="stop_circle").props("outline")
            else:
                ui.label(_tr(t, "ui.web.external.empty", "当前没有发现外部终端里手动启动的 Agent 进程。")).classes("cb-muted")
