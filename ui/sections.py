from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Callable, Protocol
from urllib.parse import quote, unquote

try:
    from typing import Self
except ImportError:  # Python 3.10 compatibility
    from typing_extensions import Self

from agent_backends import supported_backend_options
from core.navigation import DIAGNOSTICS_PAGE, HOME_PAGE, MOBILE_PAGE, SESSIONS_PAGE, STREAM_PAGE
from core.view_models import WebConsoleViewModel


Translator = Callable[..., str]
STREAM_MANUAL_HISTORY_LIMIT = 60


class UIEventLike(Protocol):
    value: object


class UIElementLike(Protocol):
    value: object
    text: str

    def __enter__(self) -> Self: ...
    def __exit__(self, exc_type, exc, tb) -> bool | None: ...
    def classes(self, value: str) -> Self: ...
    def props(self, value: str) -> Self: ...
    def style(self, value: str) -> Self: ...
    def set_enabled(self, value: bool) -> Self: ...
    def set_source(self, value: str) -> Self: ...
    def on_value_change(self, handler: Callable[[UIEventLike], None]) -> Self: ...
    def set_value(self, value: object) -> Self: ...
    def open(self) -> None: ...
    def close(self) -> None: ...
    def deactivate(self) -> None: ...


class UIFactoryLike(Protocol):
    def add_body_html(self, html: str) -> None: ...
    def column(self) -> UIElementLike: ...
    def row(self) -> UIElementLike: ...
    def card(self) -> UIElementLike: ...
    def label(self, text: str = "") -> UIElementLike: ...
    def markdown(self, content: str) -> UIElementLike: ...
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
    def image(self, source: str) -> UIElementLike: ...
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
        ui.label(title).classes("cb-page-title cb-ink")
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


def _dialog_card(ui: UIFactoryLike, classes: str = "") -> UIElementLike:
    return ui.card().classes(f"cb-card w-[28rem] max-w-[calc(100vw-1rem)] p-5 {classes}".strip())


def _render_disclosure_code(ui: UIFactoryLike, title: str, content: str) -> None:
    with ui.element("details").classes("cb-disclosure cb-panel w-full p-4"):
        with ui.element("summary").classes("flex items-center justify-between gap-3 font-semibold cb-ink"):
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
                        ui.label(row.name).classes("text-lg font-bold cb-ink break-all")
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
                        with ui.element("div").classes("cb-panel w-auto min-w-[5.5rem] px-3 py-2"):
                            ui.label(label).classes("cb-stat-label")
                            ui.label(str(value)).classes("text-base font-bold cb-ink")


def _render_task_summary_cards(ui: UIFactoryLike, model: WebConsoleViewModel, t: Translator, on_select_task) -> None:
    with ui.column().classes("w-full gap-3"):
        for task in model.tasks:
            with _panel(ui):
                with ui.row().classes("w-full items-start justify-between gap-3 flex-wrap"):
                    with ui.column().classes("gap-1 grow"):
                        ui.label(f"{task.agent_name} / {task.status}").classes("text-base font-bold cb-ink")
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
                        ui.label(task.prompt_summary).classes("text-sm cb-ink")
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
    on_refresh_checks,
    on_submit_task,
    on_switch_account,
    on_set_weixin_notice_enabled,
    on_open_qr_login,
    on_open_qq_login,
    bridge_mode: str = "weixin",
    on_set_bridge_mode=None,
) -> None:
    active_mode = bridge_mode if bridge_mode in {"weixin", "qq"} else "weixin"
    set_mode = on_set_bridge_mode or (lambda _mode: None)
    with ui.element("section").props(f"id={HOME_PAGE.anchor}").classes("w-full"):
        _render_page_intro(ui, _tr(t, "ui.tab.home", HOME_PAGE.title), _tr(t, "ui.page.home.description", HOME_PAGE.description), "Console")
        with _responsive_grid(ui, "grid-cols-1"):
            with ui.card().classes("cb-card w-full p-5"):
                with ui.row().classes("items-center justify-between gap-2"):
                    _render_card_title(ui, _tr(t, "ui.web.home.service_controls", "服务控制"))
                    ui.button(_tr(t, "ui.web.action.refresh_status", "刷新状态"), on_click=on_refresh_checks, icon="refresh").props("outline")
                with ui.row().classes("gap-2 pb-4 flex-wrap"):
                    ui.button(
                        _tr(t, "ui.web.mode.weixin", "微信模式"),
                        on_click=lambda: set_mode("weixin"),
                        icon="chat",
                    ).props("color=primary unelevated" if active_mode == "weixin" else "outline")
                    ui.button(
                        _tr(t, "ui.web.mode.qq", "QQ 模式"),
                        on_click=lambda: set_mode("qq"),
                        icon="forum",
                    ).props("color=primary unelevated" if active_mode == "qq" else "outline")
                status_panel_class, badge_class = _status_variant(f"{model.home.badge_text} {model.home.summary_text}")
                with ui.element("div").classes(f"cb-status-panel {status_panel_class} w-full mb-4"):
                    with ui.row().classes("w-full items-start justify-between gap-3 flex-wrap"):
                        with ui.column().classes("gap-2 grow"):
                            ui.label(_tr(t, "ui.web.home.system_status", "系统状态")).classes("cb-kicker")
                            ui.label(model.home.summary_text).classes("text-base font-bold cb-ink")
                            ui.label(
                                model.home.primary_hint
                            ).classes("text-sm cb-muted")
                        ui.label(model.home.badge_text).classes(f"{badge_class} self-start")
                with ui.row().classes("gap-2 pt-4 flex-wrap"):
                    if active_mode == "weixin":
                        ui.button(_tr(t, "ui.primary.start.label", "启动服务"), on_click=lambda: on_run_action("start-weixin"), icon="play_arrow")
                        ui.button(_tr(t, "ui.primary.stop.label", "停止服务"), on_click=lambda: on_run_action("stop"), icon="stop")
                        ui.button(_tr(t, "ui.web.action.restart", "重启服务"), on_click=lambda: on_run_action("restart"), icon="restart_alt")
                    else:
                        ui.button(_tr(t, "ui.primary.start.label", "启动服务"), on_click=lambda: on_run_action("start-qq-bridge"), icon="play_arrow")
                        ui.button(_tr(t, "ui.primary.stop.label", "停止服务"), on_click=lambda: on_run_action("stop"), icon="stop")
                        ui.button(_tr(t, "ui.web.action.restart", "重启服务"), on_click=lambda: on_run_action("restart-qq-stack"), icon="restart_alt")
                        ui.button(_tr(t, "ui.web.action.restart_onebot_runtime", "重启 QQ OneBot"), on_click=lambda: on_run_action("restart-onebot-runtime"), icon="restart_alt").props("outline")
                    ui.button(_tr(t, "ui.web.action.restart_hub", "只重启 Hub"), on_click=lambda: on_run_action("restart-hub"), icon="sync").props("outline")
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
                _render_card_title(
                    ui,
                    _tr(t, "ui.web.home.accounts", "账号管理"),
                    _tr(t, "ui.web.mode.qq_detail", "用于 QQ 私聊/群聊的 OneBot 入口；切换后微信桥会停止。") if active_mode == "qq" else "",
                )
                if active_mode == "qq":
                    with _panel(ui):
                        ui.label(_tr(t, "ui.web.qq.account", "QQ 当前账号：{account}", account=model.home.qq_account_label)).classes("cb-chip cb-chip-ok w-fit" if model.home.qq_login_ok else "cb-chip cb-chip-warn w-fit")
                        ui.label(_tr(t, "ui.web.account.login_status", "登录状态：{status}", status=model.home.qq_login_status_text)).classes("text-sm cb-muted")
                        if model.home.qq_login_detail_text:
                            ui.label(model.home.qq_login_detail_text).classes("text-sm text-orange-700")
                        ui.label(_tr(t, "ui.qq_login.onebot_api", "OneBot HTTP API: http://127.0.0.1:3000")).classes("text-sm cb-muted")
                        ui.label(_tr(t, "ui.qq_login.reverse_http", "反向 HTTP 上报: http://127.0.0.1:5701/")).classes("text-sm cb-muted")
                    with ui.row().classes("gap-2 flex-wrap pt-4"):
                        ui.button(_tr(t, "ui.qq_login.button", "扫码登录 QQ"), on_click=on_open_qq_login, icon="qr_code_scanner")
                else:
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

            if active_mode == "weixin":
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
                    ui.label(item.label).classes("font-semibold cb-ink")
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
    on_load_session_rows,
    on_set_session_page,
    on_load_session_files,
    on_load_session_detail,
    on_select_task,
    on_load_task_list,
    on_set_task_page,
    on_load_task_detail,
    on_set_task_filters,
    on_find_task_by_id,
    on_open_weixin_binding,
    on_open_weixin_binding_task,
    on_load_weixin_bindings,
    on_switch_weixin_binding_backend,
    on_reset_weixin_binding,
) -> None:
    with ui.element("section").props(f"id={SESSIONS_PAGE.anchor}").classes("w-full"):
        _render_page_intro(ui, _tr(t, "ui.tab.sessions", SESSIONS_PAGE.title), _tr(t, "ui.page.sessions.description", SESSIONS_PAGE.description), "Sessions")
        with _responsive_grid(ui, "grid-cols-1 xl:grid-cols-2"):
            with ui.card().classes("cb-card w-full p-5"):
                _render_card_title(ui, _tr(t, "ui.web.sessions.overview", "会话概览"))
                if not model.session_rows_loaded:
                    ui.label(_tr(t, "ui.web.sessions.lazy_rows", "点击“加载会话列表”后再读取最近任务并生成会话概览。")).classes("text-sm cb-muted")
                    ui.button(_tr(t, "ui.web.action.load_session_rows", "加载会话列表"), on_click=on_load_session_rows, icon="download").props("outline")
                else:
                    if not model.session_files_loaded:
                        ui.label(_tr(t, "ui.web.sessions.lazy_files", "当前只显示最近任务涉及的会话；点击后再读取历史会话文件。")).classes("text-sm cb-muted")
                        ui.button(_tr(t, "ui.web.action.load_session_files", "加载历史会话"), on_click=on_load_session_files, icon="download").props("outline")
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
            if not model.task_list_loaded:
                ui.label(_tr(t, "ui.web.tasks.lazy_list", "点击“加载任务列表”后再读取最近任务和筛选项。")).classes("text-sm cb-muted")
                ui.button(_tr(t, "ui.web.action.load_task_list", "加载任务列表"), on_click=on_load_task_list, icon="download").props("outline")
            else:
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
            if not model.weixin_bindings_loaded:
                ui.label(_tr(t, "ui.web.bindings.lazy_list", "点击“加载微信会话绑定”后再读取发送方会话状态。")).classes("text-sm cb-muted")
                ui.button(_tr(t, "ui.web.action.load_weixin_bindings", "加载微信会话绑定"), on_click=on_load_weixin_bindings, icon="download").props("outline")
            elif model.weixin_conversations:
                for item in model.weixin_conversations:
                    with _panel(ui):
                        with ui.dialog() as reset_dialog, _dialog_card(ui):
                            ui.label(_tr(t, "ui.web.bindings.reset_title", "确认重置微信会话")).classes("text-lg font-semibold")
                            ui.label(_tr(t, "ui.web.bindings.reset_body", "这会删除该发送方的会话状态，并在 Bridge 运行中时自动重启使其生效。")).classes("text-sm cb-muted")
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
                            with ui.column().classes("gap-1 min-w-0 flex-1"):
                                ui.label(_tr(t, "ui.web.bindings.sender", "发送方: {sender}", sender=item.sender_id)).classes("font-semibold break-all")
                                ui.label(_tr(t, "ui.web.bindings.current", "Agent: {agent} | 当前会话: {session} | 当前后端: {backend}", agent=item.agent_id, session=item.current_session, backend=item.current_backend)).classes("text-sm cb-ink break-all")
                                ui.label(_tr(t, "ui.web.bindings.count", "会话数: {count} | 最近更新: {updated_at}", count=item.session_count, updated_at=item.updated_at)).classes("text-sm cb-muted break-all")
                                if item.latest_task_id:
                                    ui.label(_tr(t, "ui.web.bindings.latest_task", "最近任务: {task} [{status}]", task=item.latest_task_id, status=item.latest_task_status)).classes("text-sm cb-muted break-all")
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


def render_mobile_section(
    ui: UIFactoryLike,
    t: Translator,
    mobile_url: str,
    qr_data_url: str,
    on_copy_mobile_url,
    on_open_mobile_url,
) -> None:
    with ui.element("section").props(f"id={MOBILE_PAGE.anchor}").classes("w-full"):
        _render_page_intro(ui, _tr(t, "ui.tab.mobile", MOBILE_PAGE.title), _tr(t, "ui.page.mobile.description", MOBILE_PAGE.description), "Mobile")
        with _responsive_grid(ui, "grid-cols-1 lg:grid-cols-[minmax(18rem,24rem)_1fr]").classes("cb-mobile-qr-panel"):
            with ui.card().classes("cb-card w-full p-5"):
                _render_card_title(
                    ui,
                    _tr(t, "ui.web.mobile.qr_title", "扫码打开手机看板"),
                    _tr(t, "ui.web.mobile.qr_detail", "手机与电脑在同一 WiFi 下时，用相机或扫码工具对准二维码即可打开。"),
                )
                with ui.element("div").classes("w-full flex justify-center py-2"):
                    ui.image(qr_data_url).classes("w-64 h-64 rounded-[8px] border border-[var(--cb-border)] bg-white p-2")
                with ui.row().classes("gap-2 flex-wrap justify-center"):
                    ui.button(_tr(t, "ui.web.action.copy_mobile_url", "复制地址"), on_click=on_copy_mobile_url, icon="content_copy").props("outline")
                    ui.button(_tr(t, "ui.web.action.open_mobile_url", "打开手机页"), on_click=on_open_mobile_url, icon="open_in_new")
            with ui.card().classes("cb-card w-full p-5"):
                _render_card_title(
                    ui,
                    _tr(t, "ui.web.mobile.access_title", "局域网访问地址"),
                    _tr(t, "ui.web.mobile.access_detail", "这个地址包含访问 token，请只在可信局域网内使用。"),
                )
                _render_code_block(ui, mobile_url, "text-sm")
                with _responsive_grid(ui, "grid-cols-1 md:grid-cols-2"):
                    with _panel(ui):
                        ui.label(_tr(t, "ui.web.mobile.step_scan", "1. 用手机扫码")).classes("font-semibold cb-ink")
                        ui.label(_tr(t, "ui.web.mobile.step_scan_detail", "看到链接提示后点开，会进入 ChatBridge Mobile。")).classes("text-sm cb-muted")
                    with _panel(ui):
                        ui.label(_tr(t, "ui.web.mobile.step_home", "2. 添加到主屏幕")).classes("font-semibold cb-ink")
                        ui.label(_tr(t, "ui.web.mobile.step_home_detail", "浏览器打开后可添加到主屏幕，之后像 App 一样进入。")).classes("text-sm cb-muted")
                ui.label(_tr(t, "ui.web.mobile.tcp_note", "当前是局域网 HTTP over TCP 访问，不需要域名，也没有使用 NATFRP HTTP 隧道。")).classes("text-sm cb-muted")


def _stream_text(value: object, *, limit: int = 1800) -> str:
    text = str(value or "").strip("\r\n")
    return text if len(text) <= limit else f"{text[: limit - 1]}..."


_STREAM_INLINE_CODE_RE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
_STREAM_FENCED_CODE_RE = re.compile(
    r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})[^\n]*\n.*?^[ \t]{0,3}(?P=fence)[ \t]*$",
    re.DOTALL | re.MULTILINE,
)
_STREAM_INDENTED_CODE_LINE_RE = re.compile(r"^(?: {4}|\t)")
_STREAM_LOCAL_MOBILE_URL_RE = re.compile(r"https?://(?:localhost|127(?:\.\d{1,3}){3}|\[::1\])(?::\d+)?(?=/mobile-upload/)")
_STREAM_MARKDOWN_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[ \t]*(?:markdown|md)[^\n]*\n(?P<body>.*)\n[ \t]*(?:`{3,}|~{3,})[ \t]*$", re.DOTALL | re.IGNORECASE)
_STREAM_MARKDOWN_IMAGE_LINE_RE = re.compile(r"^[ \t]*!\[[^\]\n]*\]\([^)]+\)[ \t]*$")
_STREAM_LINE_SUFFIX_RE = re.compile(r"(#L\d+(?:-L?\d+)?|:\d+(?::\d+)?(?:-\d+(?::\d+)?)?|\(\d+(?:,\d+)?(?:-\d+(?:,\d+)?)?\))$")
_STREAM_FILE_EXTENSIONS = {
    "astro",
    "bash",
    "c",
    "cc",
    "cjs",
    "cpp",
    "cs",
    "css",
    "cts",
    "cxx",
    "env",
    "fish",
    "go",
    "gql",
    "gradle",
    "graphql",
    "h",
    "hpp",
    "htm",
    "html",
    "ini",
    "java",
    "js",
    "json",
    "jsonc",
    "jsx",
    "kt",
    "kts",
    "less",
    "lock",
    "lua",
    "md",
    "mdx",
    "mjs",
    "mts",
    "php",
    "proto",
    "py",
    "rb",
    "rs",
    "sass",
    "scss",
    "sh",
    "sql",
    "svelte",
    "swift",
    "toml",
    "ts",
    "tsx",
    "txt",
    "vue",
    "xml",
    "yaml",
    "yml",
    "zsh",
}


def _stream_markdown(value: str, t: Translator) -> str:
    copy_title = _tr(t, "ui.web.mobile.copy_file_path", "复制文件路径")
    text = str(value or "")
    parts: list[str] = []
    cursor = 0
    for match in _STREAM_FENCED_CODE_RE.finditer(text):
        if match.start() > cursor:
            parts.append(_stream_markdown_text_segment(text[cursor:match.start()], copy_title))
        parts.append(_stream_markdown_fenced_segment(match.group(0), copy_title))
        cursor = match.end()
    if cursor < len(text):
        parts.append(_stream_markdown_text_segment(text[cursor:], copy_title))
    return "".join(parts)

def _stream_markdown_fenced_segment(value: str, copy_title: str) -> str:
    match = _STREAM_MARKDOWN_FENCE_RE.match(value)
    if not match:
        return value
    image_lines = [
        line.strip()
        for line in str(match.group("body") or "").splitlines()
        if _STREAM_MARKDOWN_IMAGE_LINE_RE.match(line)
    ]
    if not image_lines:
        return value
    return f"{value}\n\n{_stream_markdown_text_segment(chr(10).join(image_lines), copy_title)}"

def _stream_markdown_text_segment(value: str, copy_title: str) -> str:
    parts: list[str] = []
    text_lines: list[str] = []

    def flush_text_lines() -> None:
        if not text_lines:
            return
        parts.append(_stream_markdown_inline_segment("".join(text_lines), copy_title))
        text_lines.clear()

    for line in value.splitlines(keepends=True):
        if _STREAM_INDENTED_CODE_LINE_RE.match(line):
            flush_text_lines()
            parts.append(line)
        else:
            text_lines.append(line)
    flush_text_lines()
    return "".join(parts)


def _stream_markdown_inline_segment(value: str, copy_title: str) -> str:
    parts: list[str] = []
    cursor = 0
    for match in _STREAM_INLINE_CODE_RE.finditer(value):
        if match.start() > cursor:
            segment = _stream_rewrite_local_mobile_urls(value[cursor:match.start()])
            parts.append(_stream_rewrite_markdown_links(segment, copy_title))
        parts.append(_stream_file_link_replacement(match, copy_title))
        cursor = match.end()
    if cursor < len(value):
        segment = _stream_rewrite_local_mobile_urls(value[cursor:])
        parts.append(_stream_rewrite_markdown_links(segment, copy_title))
    return "".join(parts)

def _stream_rewrite_local_mobile_urls(value: str) -> str:
    return _STREAM_LOCAL_MOBILE_URL_RE.sub("", value)

def _stream_rewrite_markdown_links(value: str, copy_title: str) -> str:
    parts: list[str] = []
    cursor = 0
    while cursor < len(value):
        start = value.find("[", cursor)
        if start < 0:
            parts.append(value[cursor:])
            break
        label_end = _stream_find_markdown_label_end(value, start)
        if label_end < 0 or label_end + 1 >= len(value) or value[label_end + 1] != "(":
            parts.append(value[cursor:start + 1])
            cursor = start + 1
            continue
        parsed = _stream_parse_markdown_link_destination(value, label_end + 2)
        if parsed is None:
            parts.append(value[cursor:start + 1])
            cursor = start + 1
            continue
        destination, end = parsed
        if start > 0 and value[start - 1] == "!":
            rewritten = _stream_markdown_image_destination(destination)
            if rewritten != destination:
                parts.append(value[cursor:label_end + 2])
                parts.append(rewritten)
                parts.append(")")
            else:
                parts.append(value[cursor:end + 1])
            cursor = end + 1
            continue
        href = _stream_markdown_href_candidate(destination)
        if href and _stream_is_file_href_candidate(href, allow_spaces=True):
            parts.append(value[cursor:start])
            parts.append(_stream_markdown_file_link_replacement(value[start + 1:label_end], href, copy_title))
            cursor = end + 1
            continue
        parts.append(value[cursor:end + 1])
        cursor = end + 1
    return "".join(parts)

def _stream_markdown_image_destination(destination: str) -> str:
    href = _stream_markdown_href_candidate(destination)
    if href.lower().startswith(("data:image/", "http://", "https://", "/mobile-upload/", "/mobile-codex-image/")):
        return href
    try:
        from ui.mobile import _image_preview_payload
    except ImportError:
        return destination
    preview = _image_preview_payload(href)
    source = str(preview.get("source") or "").strip()
    return source or destination


def _stream_find_markdown_label_end(value: str, start: int) -> int:
    depth = 0
    cursor = start + 1
    while cursor < len(value):
        char = value[cursor]
        if char == "\\":
            cursor += 2
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            if depth == 0:
                return cursor
            depth -= 1
        cursor += 1
    return -1

def _stream_parse_markdown_link_destination(value: str, start: int) -> tuple[str, int] | None:
    if start >= len(value):
        return None
    if value[start] == "<":
        end_angle = value.find(">", start + 1)
        if end_angle < 0:
            return None
        cursor = end_angle + 1
        while cursor < len(value) and value[cursor].isspace():
            cursor += 1
        if cursor < len(value) and value[cursor] in {"'", '"'}:
            quote_char = value[cursor]
            cursor += 1
            while cursor < len(value) and value[cursor] != quote_char:
                cursor += 1
            if cursor >= len(value):
                return None
            cursor += 1
            while cursor < len(value) and value[cursor].isspace():
                cursor += 1
        if cursor >= len(value) or value[cursor] != ")":
            return None
        return value[start:end_angle + 1], cursor
    depth = 0
    cursor = start
    while cursor < len(value):
        char = value[cursor]
        if char == "\n":
            return None
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                return value[start:cursor], cursor
            depth -= 1
        cursor += 1
    return None


def _stream_markdown_href_candidate(value: str) -> str:
    href = str(value or "").strip()
    if href.startswith("<") and href.endswith(">"):
        return unquote(href[1:-1].strip())
    decoded = unquote(href)
    if _stream_is_file_href_candidate(decoded, allow_spaces=True):
        return decoded
    title_match = re.match(r"(?P<href>.+?)\s+(['\"])[^'\"]*\2$", decoded)
    if title_match:
        return title_match.group("href").strip()
    return decoded


def _stream_markdown_file_link_replacement(label: str, href: str, copy_title: str) -> str:
    encoded_href = f"#chatbridge-file={quote(href, safe='')}"
    title = copy_title.replace('"', "'")
    return f"[{label}]({encoded_href} \"{title}\")"


def _stream_file_link_replacement(match: re.Match[str], copy_title: str) -> str:
    token = match.group(1)
    if not _stream_is_file_path_candidate(token):
        return match.group(0)
    href = f"#chatbridge-file={quote(token, safe='')}"
    title = copy_title.replace('"', "'")
    return f"[`{token}`]({href} \"{title}\")"


def _stream_is_file_href_candidate(value: str, *, allow_spaces: bool = False) -> bool:
    href = str(value or "").strip()
    if not href:
        return False
    if href.lower().startswith("file://"):
        return True
    return _stream_is_file_path_candidate(href, allow_spaces=allow_spaces)


def _stream_is_file_path_candidate(value: str, *, allow_spaces: bool = False) -> bool:
    token = str(value or "").strip()
    if not token or len(token) > 260 or "\n" in token:
        return False
    if any(char in token for char in "<>"):
        return False
    lowered = token.lower()
    if lowered.startswith(("http://", "https://", "mailto:", "data:", "chatbridge-file://")):
        return False
    if not allow_spaces and any(char.isspace() for char in token):
        return False
    normalized = token.replace("\\", "/").strip("'\"`")
    without_line = _STREAM_LINE_SUFFIX_RE.sub("", normalized)
    basename = without_line.rstrip("/").rsplit("/", 1)[-1]
    if "." not in basename:
        return False
    stem, extension = basename.rsplit(".", 1)
    if not stem:
        return False
    extension = extension.lower()
    if extension not in _STREAM_FILE_EXTENSIONS:
        return False
    if re.match(r"^[A-Za-z]:/", without_line):
        return True
    if without_line.startswith(("/", "./", "../", "~/")):
        return True
    if "/" in without_line:
        first_segment = without_line.split("/", 1)[0]
        return "." not in first_segment
    return "." in basename and extension in _STREAM_FILE_EXTENSIONS


def _stream_status_class(status: str) -> str:
    if status == "succeeded":
        return "cb-chip cb-chip-ok"
    if status == "failed":
        return "cb-chip cb-chip-danger"
    return "cb-chip cb-chip-warn"


def _stream_summary(task: dict[str, object], *, limit: int = 120) -> str:
    for key in ("summary", "progress_text", "output", "error", "prompt"):
        text = _stream_text(task.get(key), limit=limit)
        if text:
            return text
    return ""


def _stream_task_body(task: dict[str, object], t: Translator) -> tuple[str, str]:
    error = _stream_text(task.get("error"))
    if error:
        return _tr(t, "ui.web.mobile.stream_error", "错误"), error
    progress = _stream_text(task.get("progress_text"))
    if progress:
        return _tr(t, "ui.web.mobile.stream_progress", "进度"), progress
    output = _stream_text(task.get("output"))
    if output:
        return _tr(t, "ui.web.mobile.stream_output", "输出"), output
    return _tr(t, "ui.web.mobile.stream_prompt", "输入"), _stream_text(task.get("prompt"))


def _stream_reasoning_and_live_output(task: dict[str, object]) -> tuple[str, str]:
    reasoning_text = _stream_text(task.get("reasoning_text"), limit=12000)
    live_output_text = _stream_text(task.get("live_output_text"), limit=20000)
    progress_text = _stream_text(task.get("progress_text"), limit=20000)
    if live_output_text or not progress_text:
        return reasoning_text, live_output_text
    if reasoning_text:
        return reasoning_text, "" if progress_text == reasoning_text else progress_text
    for prefix in ("思考：", "Thinking:"):
        if progress_text.startswith(prefix):
            return progress_text[len(prefix):].strip(), ""
    source = str(task.get("source") or "").strip()
    status = str(task.get("status") or "").strip()
    if source == "codex-app-server" and status not in {"running", "queued"}:
        return progress_text, ""
    return "", progress_text


def _stream_reasoning_preview(value: str, *, limit: int = 120) -> tuple[str, bool]:
    plain = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", str(value or ""))
    plain = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", plain)
    plain = re.sub(r"[*_~`]+", "", plain)
    plain = re.sub(r"(^|\s)[#>]+\s*", r"\1", plain)
    return _stream_tail_preview(plain, limit=limit)


def _stream_tail_preview(value: str, *, limit: int) -> tuple[str, bool]:
    compact = " ".join(str(value or "").split())
    if len(compact) <= limit:
        return compact, False
    return f"...{compact[-(limit - 3):].lstrip()}", True


def _stream_time_sort_key(value: object) -> tuple[int, float, str]:
    text = str(value or "").strip()
    if not text:
        return (1, 0.0, "")
    normalized = text.replace("Z", "+00:00")
    try:
        return (0, datetime.fromisoformat(normalized).timestamp(), text)
    except ValueError:
        pass
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return (0, datetime.strptime(text, fmt).timestamp(), text)
        except ValueError:
            continue
    return (1, 0.0, text)


def _stream_task_sort_key(task: dict[str, object]) -> tuple[tuple[int, float, str], str, str, str]:
    stream_order = task.get("stream_order")
    try:
        parsed_order = int(stream_order)
    except (TypeError, ValueError):
        parsed_order = 0
    created_at = _stream_time_sort_key(task.get("created_at"))
    task_id = str(task.get("id") or "")
    if parsed_order > 0:
        return (created_at, "0", f"{parsed_order:08d}", task_id)
    return (created_at, "1", "", task_id)


def _stream_session_order(sessions: dict[str, list[dict[str, object]]], session_names: list[str]) -> list[str]:
    return sorted(
        session_names,
        key=lambda session_name: max((_stream_task_sort_key(task) for task in sessions.get(session_name, [])), default=((1, 0.0, ""), "", "", "")),
        reverse=True,
    )


def _stream_image_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item or "").strip() for item in value if str(item or "").strip()]


def _stream_image_previews(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    previews: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "").strip()
        label = str(item.get("label") or "").strip()
        kind = str(item.get("kind") or "").strip()
        if source or label:
            previews.append({"source": source, "label": label, "kind": kind})
    return previews


def _stream_output_segments(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    segments: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        if kind == "text":
            text = _stream_text(item.get("text"), limit=20000)
            if text:
                segments.append({"kind": kind, "text": text})
        elif kind == "custom_tool_image":
            source = str(item.get("source") or "").strip()
            if source:
                segments.append(
                    {
                        "kind": kind,
                        "source": source,
                        "label": str(item.get("label") or "").strip(),
                    }
                )
    return segments


def _stream_activity_items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    hidden_events = {"accepted", "running", "progress", "succeeded"}
    items: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        event = str(item.get("event") or "").strip()
        if event in hidden_events or event.startswith("codex_"):
            continue
        activity_type = str(item.get("type") or "system").strip() or "system"
        at = str(item.get("at") or "").strip()
        detail = str(item.get("detail") or "").strip()
        raw_metadata = item.get("metadata")
        metadata = {
            str(key): str(value).strip()
            for key, value in raw_metadata.items()
            if isinstance(raw_metadata, dict) and str(key).strip() and str(value).strip()
        } if isinstance(raw_metadata, dict) else {}
        if event and at:
            items.append({"event": event, "type": activity_type, "at": at, "detail": detail, "metadata": metadata})
    return items


def _stream_command_items(value: object, *, task_status: str = "") -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    terminal_task = task_status in {"succeeded", "failed", "canceled", "unknown_after_restart"}
    items: list[dict[str, object]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict) or str(item.get("event") or "") != "codex_command":
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        command = str(metadata.get("command") or item.get("detail") or "").strip()
        if not command:
            continue
        raw_status = str(metadata.get("status") or "inProgress").replace("_", "").lower()
        status = "running" if raw_status in {"inprogress", "running"} else "failed" if raw_status in {"failed", "declined"} else "completed"
        exit_code = metadata.get("exit_code") if "exit_code" in metadata else metadata.get("exitCode")
        if str(exit_code or "").lstrip("-").isdigit() and int(str(exit_code)) != 0:
            status = "failed"
        if terminal_task and status == "running":
            status = "interrupted"
        items.append(
            {
                "id": str(item.get("id") or f"command-{index + 1}"),
                "command": command,
                "cwd": str(metadata.get("cwd") or "").strip(),
                "status": status,
                "output": str(metadata.get("output") or metadata.get("aggregatedOutput") or "")[-12000:],
                "exit_code": "" if exit_code is None else str(exit_code),
                "duration_ms": str(metadata.get("duration_ms") or metadata.get("durationMs") or "").strip(),
                "at": str(item.get("at") or "").strip(),
            }
        )
    return items[-24:]


def _stream_command_status_label(status: str, t: Translator) -> str:
    return {
        "running": _tr(t, "ui.web.mobile.command_running", "正在运行命令"),
        "completed": _tr(t, "ui.web.mobile.command_completed", "已运行命令"),
        "failed": _tr(t, "ui.web.mobile.command_failed", "命令运行失败"),
        "interrupted": _tr(t, "ui.web.mobile.command_interrupted", "命令已中断"),
    }.get(status, _tr(t, "ui.web.mobile.command_activity", "命令活动"))


def _render_stream_command_item(ui: UIFactoryLike, t: Translator, item: dict[str, object], *, task_id: str, index: int) -> None:
    status = str(item.get("status") or "running")
    command = str(item.get("command") or "")
    output = str(item.get("output") or "")
    command_preview = " ".join(command.split())
    if len(command_preview) > 160:
        command_preview = f"{command_preview[:157].rstrip()}..."
    output_preview, _ = _stream_tail_preview(output, limit=180)
    command_key = quote(f"{task_id}:{item.get('id') or index}", safe="")
    with ui.element("details").props(f"data-command-details=1 data-command-key={command_key}").classes(f"cb-stream-command cb-stream-command-{status}"):
        with ui.element("summary").classes("cb-stream-command-summary"):
            ui.element("span").classes("cb-stream-command-status-icon")
            with ui.element("div").classes("cb-stream-command-heading"):
                ui.label(_stream_command_status_label(status, t)).classes("cb-stream-command-label")
                ui.label(command_preview).classes("cb-stream-command-command-preview")
                if output_preview:
                    ui.label(output_preview).classes("cb-stream-command-preview")
            with ui.element("span").classes("cb-stream-command-toggle"):
                ui.label(_tr(t, "ui.web.mobile.stream_reasoning_expand", "展开")).classes("cb-stream-command-toggle-label cb-stream-command-toggle-label-open")
                ui.label(_tr(t, "ui.web.mobile.stream_reasoning_collapse", "收起")).classes("cb-stream-command-toggle-label cb-stream-command-toggle-label-close")
                ui.element("span").classes("cb-stream-command-chevron")
        with ui.element("div").classes("cb-stream-command-body"):
            ui.label(_tr(t, "ui.web.mobile.command_command", "命令")).classes("cb-stream-command-section-label")
            ui.label(command).classes("cb-stream-command-code")
            if output:
                ui.label(_tr(t, "ui.web.mobile.command_output", "输出")).classes("cb-stream-command-section-label")
                ui.label(output).classes("cb-stream-command-output")
            details = []
            cwd = str(item.get("cwd") or "")
            if cwd:
                details.append(_tr(t, "ui.web.mobile.command_cwd", "目录: {value}", value=cwd))
            duration_ms = str(item.get("duration_ms") or "")
            if duration_ms:
                details.append(_tr(t, "ui.web.mobile.command_duration", "耗时: {value} ms", value=duration_ms))
            exit_code = str(item.get("exit_code") or "")
            if exit_code:
                details.append(_tr(t, "ui.web.mobile.command_exit_code", "退出码: {value}", value=exit_code))
            if details:
                ui.label(" · ".join(details)).classes("cb-stream-command-meta")


def _stream_activity_type_class(activity_type: str) -> str:
    if activity_type == "success":
        return "cb-stream-activity-item cb-stream-activity-success"
    if activity_type == "error":
        return "cb-stream-activity-item cb-stream-activity-error"
    if activity_type == "info":
        return "cb-stream-activity-item cb-stream-activity-info"
    return "cb-stream-activity-item cb-stream-activity-system"


def _stream_activity_message(item: dict[str, object], t: Translator) -> str:
    event = str(item.get("event") or "")
    return {
        "accepted": _tr(t, "ui.web.mobile.activity_accepted", "已接收任务"),
        "running": _tr(t, "ui.web.mobile.activity_running", "开始运行"),
        "progress": _tr(t, "ui.web.mobile.activity_progress", "收到进度"),
        "succeeded": _tr(t, "ui.web.mobile.activity_succeeded", "任务完成"),
        "failed": _tr(t, "ui.web.mobile.activity_failed", "任务失败"),
        "canceled": _tr(t, "ui.web.mobile.activity_canceled", "任务已取消"),
        "unknown_after_restart": _tr(t, "ui.web.mobile.activity_restart_interrupted", "重启后状态未知"),
        "codex_tool_call": _tr(t, "ui.web.mobile.activity_codex_tool_call", "工具调用"),
        "codex_todo": _tr(t, "ui.web.mobile.activity_codex_todo", "待办更新"),
        "codex_activity": _tr(t, "ui.web.mobile.activity_codex_activity", "Codex 活动"),
        "codex_compaction": _tr(t, "ui.web.mobile.activity_codex_compaction", "上下文压缩"),
        "codex_error": _tr(t, "ui.web.mobile.activity_codex_error", "Codex 错误"),
        "codex_item": _tr(t, "ui.web.mobile.activity_codex_item", "Codex 事件"),
    }.get(event, event or _tr(t, "ui.web.mobile.activity_event", "任务事件"))


def _stream_activity_metadata_text(metadata: dict[str, object]) -> str:
    return json.dumps(metadata, ensure_ascii=False, indent=2, default=str)


def _stream_has_codex_activity(items: list[dict[str, object]]) -> bool:
    return any(str(item.get("event") or "").startswith("codex_") for item in items)

def _stream_image_is_previewable(value: str) -> bool:
    return value.lower().startswith(("data:image/", "http://", "https://", "/mobile-upload/", "/mobile-local-image/", "/mobile-codex-image/"))


def _stream_attachment_label(value: str) -> str:
    cleaned = value.replace("\\", "/").rstrip("/")
    return cleaned.rsplit("/", 1)[-1] or value


def _stream_lightbox_props(source: str, label: str, t: Translator) -> str:
    encoded_source = quote(source, safe="")
    encoded_label = quote(label, safe="")
    preview_title = quote(_tr(t, "ui.web.mobile.open_image_preview", "打开图片预览"), safe="")
    return f"data-lightbox-src={encoded_source} data-lightbox-label={encoded_label} title={preview_title} role=button tabindex=0"


def _render_stream_custom_tool_image(ui: UIFactoryLike, t: Translator, preview: dict[str, str]) -> None:
    preview_source = str(preview.get("source") or "").strip()
    if not preview_source:
        return
    preview_label = str(preview.get("label") or "").strip() or _stream_attachment_label(preview_source)
    with ui.element("details").classes("cb-stream-tool-image-details"):
        with ui.element("summary").classes("cb-stream-tool-image-summary"):
            ui.label(_tr(t, "ui.web.mobile.view_image", "查看图片"))
        ui.image(preview_source).props(_stream_lightbox_props(preview_source, preview_label, t)).classes("cb-stream-image-attachment cb-stream-image-lightbox-trigger")


def _stream_context_left_percent(value: object) -> int | None:
    if value is None:
        return None
    try:
        percent = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, min(100, percent))

def _stream_session_task_count(mobile_state: dict[str, object], session_name: str) -> int:
    counts = mobile_state.get("session_task_counts")
    if not isinstance(counts, dict):
        return 0
    value = counts.get(session_name)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0

def _stream_task_uses_utc_naive_time(task: dict[str, object]) -> bool:
    return False

def _stream_footer_time(task: dict[str, object], status: str) -> str:
    if status in {"running", "queued"}:
        return str(task.get("started_at") or task.get("created_at") or "").strip()
    return str(task.get("finished_at") or task.get("progress_at") or task.get("created_at") or "").strip()


def _parse_stream_time(value: object, *, assume_utc_naive: bool = False) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None:
        for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if assume_utc_naive and parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed

def _stream_display_time(value: object, *, assume_utc_naive: bool = False) -> str:
    text = str(value or "").strip()
    parsed = _parse_stream_time(text, assume_utc_naive=assume_utc_naive)
    if parsed is None or parsed.tzinfo is None:
        return text
    return parsed.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")


def _stream_client_time(value: object, *, assume_utc_naive: bool = False) -> str:
    text = str(value or "").strip()
    parsed = _parse_stream_time(text, assume_utc_naive=assume_utc_naive)
    if parsed is None:
        return text
    if parsed.tzinfo is None:
        return parsed.isoformat(timespec="seconds")
    return parsed.astimezone().isoformat(timespec="seconds")

def _stream_duration_text(task: dict[str, object], t: Translator) -> str:
    assume_utc_naive = _stream_task_uses_utc_naive_time(task)
    started_at = _parse_stream_time(task.get("started_at") or task.get("created_at"), assume_utc_naive=assume_utc_naive)
    finished_at = _parse_stream_time(task.get("finished_at") or task.get("progress_at"), assume_utc_naive=assume_utc_naive)
    if started_at is None or finished_at is None or finished_at < started_at:
        return ""
    return _stream_duration_seconds_text(max(0, int((finished_at - started_at).total_seconds())), t)

def _stream_duration_seconds_text(total_seconds: int, t: Translator) -> str:
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return _tr(t, "ui.web.mobile.stream_duration_hours", "{hours} 小时 {minutes} 分", hours=hours, minutes=minutes)
    if minutes:
        return _tr(t, "ui.web.mobile.stream_duration_minutes", "{minutes} 分 {seconds} 秒", minutes=minutes, seconds=seconds)
    return _tr(t, "ui.web.mobile.stream_duration_seconds", "{seconds} 秒", seconds=seconds)


def _stream_live_elapsed_text(task: dict[str, object], t: Translator) -> str:
    started_at = _parse_stream_time(
        task.get("started_at") or task.get("created_at"),
        assume_utc_naive=_stream_task_uses_utc_naive_time(task),
    )
    if started_at is None:
        return ""
    now = datetime.now(started_at.tzinfo) if started_at.tzinfo else datetime.now()
    if now < started_at:
        return _stream_duration_seconds_text(0, t)
    return _stream_duration_seconds_text(max(0, int((now - started_at).total_seconds())), t)

def _stream_footer_label(task: dict[str, object], status: str, t: Translator) -> str:
    status_text = _tr(t, f"bridge.task.status.{status}", status)
    duration_text = _stream_duration_text(task, t)
    time_text = _stream_display_time(_stream_footer_time(task, status), assume_utc_naive=_stream_task_uses_utc_naive_time(task))
    if duration_text and status not in {"running", "queued"}:
        if time_text:
            return _tr(
                t,
                "ui.web.mobile.stream_turn_footer_duration_time",
                "耗时 {duration} · {time}",
                duration=duration_text,
                time=time_text,
            )
        return _tr(
            t,
            "ui.web.mobile.stream_turn_footer_duration",
            "耗时 {duration}",
            duration=duration_text,
        )
    if not time_text:
        return status_text
    return _tr(
        t,
        "ui.web.mobile.stream_turn_footer",
        "{time}",
        time=time_text,
    )


def _stream_footer_time_label(task: dict[str, object], status: str, t: Translator) -> str:
    status_text = _tr(t, f"bridge.task.status.{status}", status)
    if _stream_duration_text(task, t) and status not in {"running", "queued"}:
        return _stream_footer_label(task, status, t)
    time_text = _stream_display_time(_stream_footer_time(task, status), assume_utc_naive=_stream_task_uses_utc_naive_time(task))
    if not time_text:
        return status_text
    return _tr(
        t,
        "ui.web.mobile.stream_turn_footer",
        "{time}",
        time=time_text,
    )


def _render_stream_footer_label(ui: UIFactoryLike, primary: str, alternate: str = "") -> None:
    alternate_text = alternate if alternate and alternate != primary else ""
    props = "data-footer-toggle=1 role=button tabindex=0" if alternate_text else ""
    with ui.element("span").props(props).classes("cb-stream-footer-label-wrap"):
        if alternate_text:
            sizer_text = primary if len(primary) >= len(alternate_text) else alternate_text
            ui.label(sizer_text).classes("cb-stream-footer-label-sizer")
            ui.label(primary).classes("cb-stream-footer-label cb-stream-footer-label-main")
            ui.label(alternate_text).classes("cb-stream-footer-label cb-stream-footer-label-alt")
        else:
            ui.label(primary).classes("cb-stream-footer-label")


def _prepare_stream_render_context(mobile_state: dict[str, object], selected_session_name: str) -> dict[str, object]:
    tasks = mobile_state.get("tasks") if isinstance(mobile_state.get("tasks"), list) else []
    visible_tasks = [task for task in tasks if isinstance(task, dict)]
    agents = mobile_state.get("agents") if isinstance(mobile_state.get("agents"), list) else []
    selected_codex_thread = mobile_state.get("selected_codex_thread") if isinstance(mobile_state.get("selected_codex_thread"), dict) else {}
    default_agent_item = next((agent for agent in agents if isinstance(agent, dict)), {})
    sessions: dict[str, list[dict[str, object]]] = {}
    session_order: list[str] = []
    for task in visible_tasks:
        session_name = str(task.get("session_name") or "default")
        if session_name not in sessions:
            sessions[session_name] = []
            session_order.append(session_name)
        sessions[session_name].append(task)
    session_order = _stream_session_order(sessions, session_order)
    selected_session = selected_session_name.strip()
    active_session = selected_session or (session_order[0] if session_order else "default")
    session_tasks = sorted(sessions.get(active_session, []), key=_stream_task_sort_key)
    has_running_session_task = any(str(task.get("status") or "").strip() == "running" for task in session_tasks)
    queued_composer_tasks = [
        task
        for task in session_tasks
        if has_running_session_task and str(task.get("status") or "").strip() == "queued"
    ]
    stream_tasks = [
        task
        for task in session_tasks
        if not (has_running_session_task and str(task.get("status") or "").strip() == "queued")
    ]
    session_total_count = _stream_session_task_count(mobile_state, active_session)
    displayed_session_count = len(session_tasks)
    has_older_session_tasks = session_total_count > max(displayed_session_count, STREAM_MANUAL_HISTORY_LIMIT)
    latest_task = session_tasks[-1] if session_tasks else None
    latest_task_id = str(latest_task.get("id") or "").strip() if isinstance(latest_task, dict) else ""
    latest_active_task = next(
        (
            task
            for task in reversed(session_tasks)
            if str(task.get("status") or "").strip() == ("running" if has_running_session_task else "queued")
            and str(task.get("id") or "").strip()
        ),
        None,
    )
    latest_active_task_id = str(latest_active_task.get("id") or "").strip() if isinstance(latest_active_task, dict) else ""
    status_task = latest_active_task if isinstance(latest_active_task, dict) else latest_task
    default_agent = str(status_task.get("agent_id") or default_agent_item.get("id") or "main") if isinstance(status_task, dict) else str(default_agent_item.get("id") or "main")
    default_backend = str(status_task.get("backend") or default_agent_item.get("backend") or "") if isinstance(status_task, dict) else str(default_agent_item.get("backend") or "")
    context_left_percent = _stream_context_left_percent(status_task.get("context_left_percent")) if isinstance(status_task, dict) else None

    return {
        "active_session": active_session,
        "session_tasks": session_tasks,
        "stream_tasks": stream_tasks,
        "queued_composer_tasks": queued_composer_tasks,
        "has_older_session_tasks": has_older_session_tasks,
        "displayed_session_count": displayed_session_count,
        "session_total_count": session_total_count,
        "is_loading": bool(selected_codex_thread.get("loading")) and not session_tasks,
        "latest_task_id": latest_task_id,
        "latest_active_task_id": latest_active_task_id,
        "default_agent": default_agent,
        "default_backend": default_backend,
        "context_left_percent": context_left_percent,
    }

def render_mobile_stream_shell(
    ui: UIFactoryLike,
    active_session: str,
    render_messages,
    render_composer,
) -> None:
    encoded_active_session = quote(active_session, safe="")
    with ui.element("section").props(f"id={STREAM_PAGE.anchor} data-stream-key={encoded_active_session} data-stream-pending=1").classes("cb-agent-panel w-full"):
        render_messages()
        _render_mobile_stream_scroll_button(ui)
        render_composer()

def render_mobile_stream_messages_section(
    ui: UIFactoryLike,
    t: Translator,
    mobile_state: dict[str, object],
    selected_session_name: str,
    on_copy_text,
    on_cancel_task,
    on_load_older,
) -> None:
    context = _prepare_stream_render_context(mobile_state, selected_session_name)
    _render_mobile_stream_messages(ui, t, context, on_copy_text, on_cancel_task, on_load_older)

def _render_mobile_stream_messages(
    ui: UIFactoryLike,
    t: Translator,
    context: dict[str, object],
    on_copy_text,
    on_cancel_task,
    on_load_older,
) -> None:
    active_session = str(context.get("active_session") or "default")
    stream_tasks = [task for task in context.get("stream_tasks", []) if isinstance(task, dict)]
    has_older_session_tasks = bool(context.get("has_older_session_tasks"))
    displayed_session_count = int(context.get("displayed_session_count") or 0)
    session_total_count = int(context.get("session_total_count") or 0)
    latest_task_id = str(context.get("latest_task_id") or "")

    with ui.element("div").props("data-stream-pending=1").classes("cb-agent-stream cb-chat-scroll"):
        with ui.column().classes("cb-agent-stream-content"):
            if has_older_session_tasks:
                load_older_label = _tr(
                    t,
                    "ui.web.mobile.load_older",
                    "加载更早消息 ({shown}/{total})",
                    shown=displayed_session_count,
                    total=session_total_count,
                )
                with ui.element("div").classes("cb-stream-load-older-wrap"):
                    ui.button(
                        load_older_label,
                        on_click=lambda session_name=active_session: on_load_older(session_name),
                        icon="expand_less",
                    ).props("flat dense data-load-older-ready=1").classes("cb-stream-load-older-button").on(
                        "click",
                        js_handler=f"""
                        () => {{
                            const scroller = document.querySelector('.cb-agent-stream');
                            if (!scroller) return;
                            window.__cbStreamLoadOlderAnchor = {{
                                key: {active_session!r},
                                scrollHeight: scroller.scrollHeight,
                                scrollTop: scroller.scrollTop,
                            }};
                        }}
                        """,
                    )
            if not stream_tasks:
                with ui.element("div").classes("cb-stream-empty-state"):
                    with ui.column().classes("items-center gap-2"):
                        if bool(context.get("is_loading")):
                            ui.label(_tr(t, "ui.web.mobile.stream_loading", "正在加载会话。")).classes("cb-stream-empty-text")
                            ui.label(_tr(t, "ui.web.mobile.stream_loading_hint", "消息会在读取完成后自动出现。")).classes("cb-stream-empty-text")
                        else:
                            ui.label(_tr(t, "ui.web.mobile.stream_empty", "暂无任务输出。")).classes("cb-stream-empty-text")
                            ui.label(_tr(t, "ui.web.mobile.stream_empty_hint", "当前会话还没有实时输出。")).classes("cb-stream-empty-text")
            else:
                for task in stream_tasks:
                    status = str(task.get("status") or "queued")
                    prompt_text = _stream_text(task.get("prompt"), limit=6000)
                    image_items = _stream_image_items(task.get("images"))
                    image_previews = _stream_image_previews(task.get("image_previews"))
                    output_image_previews = _stream_image_previews(task.get("output_image_previews"))
                    output_segments = _stream_output_segments(task.get("output_segments"))
                    error_text = _stream_text(task.get("error"), limit=8000)
                    reasoning_text, live_output_text = _stream_reasoning_and_live_output(task)
                    output_text = _stream_text(task.get("output"), limit=20000)
                    if error_text:
                        output_segments = []
                    raw_activity_items = task.get("activity_items")
                    has_codex_activity = _stream_has_codex_activity(raw_activity_items if isinstance(raw_activity_items, list) else [])
                    activity_items = _stream_activity_items(raw_activity_items)
                    command_items = _stream_command_items(raw_activity_items, task_status=status)
                    assume_utc_naive_time = _stream_task_uses_utc_naive_time(task)
                    task_id = str(task.get("id") or "").strip()
                    should_show_activity = bool(activity_items) and (
                        has_codex_activity
                        or task_id == latest_task_id
                        or status in {"running", "queued"}
                    )
                    assistant_text = error_text or output_text or live_output_text
                    is_working_placeholder = False
                    if not assistant_text and status in {"running", "queued"}:
                        assistant_text = _tr(t, "ui.web.mobile.stream_working", "正在处理")
                        is_working_placeholder = True
                    assistant_has_content = bool(assistant_text or output_segments or reasoning_text or command_items)
                    turn_classes = "cb-stream-turn cb-stream-turn-with-footer" if assistant_has_content or should_show_activity else "cb-stream-turn"
                    with ui.element("div").classes(turn_classes):
                        if prompt_text:
                            with ui.element("div").classes("cb-stream-message cb-stream-user"):
                                with ui.element("div").classes("cb-stream-user-content"):
                                    with ui.element("div").classes("cb-stream-user-bubble"):
                                        if image_items:
                                            with ui.element("div").classes("cb-stream-attachments"):
                                                for index, image_item in enumerate(image_items):
                                                    preview = image_previews[index] if index < len(image_previews) else {}
                                                    preview_source = preview.get("source") or image_item
                                                    preview_label = preview.get("label") or _stream_attachment_label(image_item)
                                                    if _stream_image_is_previewable(preview_source) or preview_source.startswith("/mobile-upload/"):
                                                        ui.image(preview_source).props(_stream_lightbox_props(preview_source, preview_label, t)).classes("cb-stream-image-attachment cb-stream-image-lightbox-trigger")
                                                    else:
                                                        with ui.element("div").classes("cb-stream-file-attachment"):
                                                            ui.label(_tr(t, "ui.web.mobile.stream_image_attachment", "图片")).classes("cb-stream-file-kind")
                                                            ui.label(preview_label).classes("cb-stream-file-name")
                                        ui.label(prompt_text).classes("cb-stream-body")
                                    with ui.element("div").classes("cb-stream-user-footer"):
                                        created_at = str(task.get("created_at") or "").strip()
                                        if created_at:
                                            ui.label(_stream_display_time(created_at, assume_utc_naive=assume_utc_naive_time)).classes("cb-stream-footer-label")
                                        ui.button(
                                            "",
                                            on_click=lambda value=prompt_text: on_copy_text(value),
                                            icon="content_copy",
                                            color=None,
                                        ).props("flat dense round").classes("cb-stream-copy-button")
                        if assistant_has_content or should_show_activity:
                            with ui.element("div").classes("cb-stream-message cb-stream-assistant"):
                                with ui.element("div").classes("cb-stream-assistant-content"):
                                    if reasoning_text:
                                        reasoning_preview, reasoning_has_more = _stream_reasoning_preview(reasoning_text)
                                        if reasoning_has_more:
                                            reasoning_key = quote(task_id or str(task.get("id") or "reasoning"), safe="")
                                            with ui.element("details").props(f"data-reasoning-details=1 data-reasoning-key={reasoning_key}").classes("cb-stream-reasoning"):
                                                with ui.element("summary").classes("cb-stream-reasoning-summary"):
                                                    with ui.element("div").classes("cb-stream-reasoning-heading"):
                                                        ui.element("span").classes("cb-stream-reasoning-icon")
                                                        ui.label(_tr(t, "ui.web.mobile.stream_reasoning", "思考过程")).classes("cb-stream-reasoning-label")
                                                    with ui.element("span").classes("cb-stream-reasoning-toggle"):
                                                        ui.label(_tr(t, "ui.web.mobile.stream_reasoning_expand", "展开")).classes("cb-stream-reasoning-toggle-label cb-stream-reasoning-toggle-label-open")
                                                        ui.label(_tr(t, "ui.web.mobile.stream_reasoning_collapse", "收起")).classes("cb-stream-reasoning-toggle-label cb-stream-reasoning-toggle-label-close")
                                                        ui.element("span").classes("cb-stream-reasoning-chevron")
                                                    ui.label(reasoning_preview).classes("cb-stream-reasoning-preview")
                                                ui.markdown(_stream_markdown(reasoning_text, t)).classes("cb-stream-reasoning-body cb-stream-markdown")
                                        else:
                                            with ui.element("div").props("data-reasoning-preview=1").classes("cb-stream-reasoning cb-stream-reasoning-static"):
                                                with ui.element("div").classes("cb-stream-reasoning-heading"):
                                                    ui.element("span").classes("cb-stream-reasoning-icon")
                                                    ui.label(_tr(t, "ui.web.mobile.stream_reasoning", "思考过程")).classes("cb-stream-reasoning-label")
                                                ui.label(reasoning_preview).classes("cb-stream-reasoning-preview")
                                    if command_items:
                                        with ui.element("div").classes("cb-stream-command-log"):
                                            for command_index, command_item in enumerate(command_items, start=1):
                                                _render_stream_command_item(ui, t, command_item, task_id=task_id, index=command_index)
                                    if should_show_activity:
                                        with ui.element("div").classes("cb-stream-activity-log"):
                                            for item in activity_items:
                                                metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
                                                detail = str(item.get("detail") or "")
                                                at = str(item.get("at") or "")
                                                display_metadata = dict(metadata)
                                                if detail:
                                                    display_metadata = {"detail": detail, **display_metadata}
                                                if at:
                                                    display_metadata = {"at": _stream_display_time(at, assume_utc_naive=assume_utc_naive_time), **display_metadata}
                                                with ui.element("details").props("data-activity-details=1").classes(_stream_activity_type_class(str(item.get("type") or ""))):
                                                    with ui.element("summary").classes("cb-stream-activity-summary"):
                                                        ui.element("span").classes("cb-stream-activity-icon")
                                                        with ui.element("div").classes("cb-stream-activity-copy"):
                                                            ui.label(_stream_activity_message(item, t)).classes("cb-stream-activity-message")
                                                            if display_metadata:
                                                                with ui.element("div").classes("cb-stream-activity-details-row"):
                                                                    ui.label(_tr(t, "ui.web.mobile.activity_details", "详情")).classes("cb-stream-activity-details-label")
                                                                    ui.element("span").classes("cb-stream-activity-chevron")
                                                    if display_metadata:
                                                        with ui.element("div").classes("cb-stream-activity-metadata"):
                                                            ui.label(_stream_activity_metadata_text(display_metadata)).classes("cb-stream-activity-metadata-text")
                                    body_classes = "cb-stream-body"
                                    if status == "failed" and error_text:
                                        body_classes = f"{body_classes} cb-stream-error"
                                    if output_segments:
                                        for segment in output_segments:
                                            segment_kind = str(segment.get("kind") or "")
                                            if segment_kind == "text":
                                                ui.markdown(_stream_markdown(str(segment.get("text") or ""), t)).classes(f"{body_classes} cb-stream-markdown")
                                            elif segment_kind == "custom_tool_image":
                                                with ui.element("div").classes("cb-stream-output-tool-image"):
                                                    _render_stream_custom_tool_image(ui, t, segment)
                                    elif assistant_text:
                                        output_link_previews = [preview for preview in output_image_previews if preview.get("kind") != "markdown_image"]
                                        if output_link_previews:
                                            with ui.element("div").classes("cb-stream-attachments"):
                                                for preview in output_link_previews:
                                                    preview_source = preview.get("source") or ""
                                                    preview_label = preview.get("label") or _stream_attachment_label(preview_source)
                                                    preview_kind = str(preview.get("kind") or "")
                                                    if preview_kind == "custom_tool_image":
                                                        _render_stream_custom_tool_image(ui, t, preview)
                                                    elif _stream_image_is_previewable(preview_source) or preview_source.startswith(("/mobile-upload/", "/mobile-local-image/")):
                                                        ui.image(preview_source).props(_stream_lightbox_props(preview_source, preview_label, t)).classes("cb-stream-image-attachment cb-stream-image-lightbox-trigger")
                                        markdown = ui.markdown(_stream_markdown(assistant_text, t)).classes(f"{body_classes} cb-stream-markdown")
                                        if status in {"running", "queued"}:
                                            live_props = f"data-stream-live=1 data-stream-text-key={quote(task_id or str(task.get('id') or ''), safe='')}"
                                            if is_working_placeholder:
                                                live_props = f"{live_props} data-stream-placeholder=1"
                                            markdown.props(live_props).classes(f"{body_classes} cb-stream-markdown cb-stream-live-text")
                                        with ui.element("div").classes("cb-stream-turn-footer"):
                                            if status in {"running", "queued"}:
                                                task_id = str(task.get("id") or "").strip()
                                                started_at = str(task.get("started_at") or task.get("created_at") or "").strip()
                                                with ui.element("span").props("aria-hidden=true").classes("cb-stream-working-loader"):
                                                    for dot_index in range(6):
                                                        ui.element("span").classes(f"cb-stream-working-loader-dot cb-stream-working-loader-dot-{dot_index}")
                                                if started_at:
                                                    client_started_at = _stream_client_time(started_at, assume_utc_naive=assume_utc_naive_time)
                                                    ui.label(_stream_live_elapsed_text(task, t)).props(f'data-started-at="{client_started_at}"').classes("cb-stream-live-elapsed")
                                                if task_id:
                                                    stop_label = _tr(t, "ui.web.mobile.cancel_task", "停止任务")
                                                    ui.button(
                                                        "",
                                                        on_click=lambda task_id=task_id: on_cancel_task(task_id),
                                                        icon="stop",
                                                    ).props(f'flat dense round title="{stop_label}" aria-label="{stop_label}" data-task-id={task_id}').classes("cb-stream-stop-button")
                                            else:
                                                if assistant_text:
                                                    ui.button(
                                                        "",
                                                        on_click=lambda value=assistant_text: on_copy_text(value),
                                                        icon="content_copy",
                                                        color=None,
                                                    ).props("flat dense round").classes("cb-stream-copy-button")
                                                _render_stream_footer_label(
                                                    ui,
                                                    _stream_footer_label(task, status, t),
                                                    _stream_footer_time_label(task, status, t),
                                                )

def _render_mobile_stream_scroll_button(ui: UIFactoryLike) -> None:
    ui.button("", icon="keyboard_arrow_down", color=None).props("round unelevated").classes("cb-scroll-bottom-button").on(
        "click",
        js_handler="""
        () => {
            const scroller = document.querySelector('.cb-agent-stream');
            if (!scroller) return;
            const decodeStreamKey = (value) => {
                try {
                    return decodeURIComponent(value || '');
                } catch {
                    return value || '';
                }
            };
            const renderedActiveKey = decodeStreamKey(document.querySelector('.cb-agent-panel')?.dataset?.streamKey || '').trim();
            const activeKey = window.__cbStreamActiveKey || window.__cbStreamDesiredActiveKey || renderedActiveKey;
            window.__cbStreamScrollStateByKey = window.__cbStreamScrollStateByKey || {};
            window.__cbStreamScrollStateByKey[activeKey] = {
                delta: 0,
                top: Math.max(0, scroller.scrollHeight - scroller.clientHeight),
                nearBottom: true,
                userScrolledAway: false,
                restoreTopPending: false,
            };
            window.__cbStreamScrollDelta = 0;
            window.__cbStreamWasNearBottom = true;
            window.__cbStreamUserScrolledAway = false;
            const programmaticScrollers = window.__cbStreamProgrammaticScrollers;
            if (programmaticScrollers) {
                programmaticScrollers.add(scroller);
                window.requestAnimationFrame(() => programmaticScrollers.delete(scroller));
            }
            scroller.scrollTop = scroller.scrollHeight;
            document.querySelector('.cb-scroll-bottom-button')?.classList.remove('cb-scroll-bottom-button-visible');
        }
        """,
    )

def render_mobile_stream_composer_section(
    ui: UIFactoryLike,
    t: Translator,
    mobile_state: dict[str, object],
    selected_session_name: str,
    pending_image_attachments: list[dict[str, str]],
    on_send_message,
    on_cancel_task,
    on_upload_image,
    on_remove_image,
    on_new_session=None,
) -> None:
    context = _prepare_stream_render_context(mobile_state, selected_session_name)
    _render_mobile_stream_composer(
        ui,
        t,
        context,
        pending_image_attachments,
        on_send_message,
        on_cancel_task,
        on_upload_image,
        on_remove_image,
    )

def _render_mobile_stream_composer(
    ui: UIFactoryLike,
    t: Translator,
    context: dict[str, object],
    pending_image_attachments: list[dict[str, str]],
    on_send_message,
    on_cancel_task,
    on_upload_image,
    on_remove_image,
) -> None:
    active_session = str(context.get("active_session") or "default")
    queued_composer_tasks = [task for task in context.get("queued_composer_tasks", []) if isinstance(task, dict)]
    latest_active_task_id = str(context.get("latest_active_task_id") or "")
    default_agent = str(context.get("default_agent") or "main")
    default_backend = str(context.get("default_backend") or "")
    context_left_percent = context.get("context_left_percent")

    def submit_composer(input_box: UIElementLike, session: str, agent: str, backend: str) -> None:
        prompt = str(input_box.value or "")
        if not session.strip():
            return
        if not prompt.strip():
            on_send_message(prompt, session, agent, backend)
            return
        submitted = on_send_message(prompt, session, agent, backend)
        if submitted is not False:
            input_box.set_value("")

    composer_zone_classes = "cb-composer-zone" if active_session else "cb-composer-zone hidden"
    with ui.element("div").classes(composer_zone_classes):
        with ui.element("div").classes("cb-composer-inner"):
            with ui.element("div").classes("cb-composer-box"):
                if queued_composer_tasks:
                    with ui.element("div").classes("cb-composer-queue-track"):
                        for queued_task in queued_composer_tasks:
                            queued_prompt = _stream_text(queued_task.get("prompt"), limit=220)
                            queued_task_id = str(queued_task.get("id") or "").strip()
                            with ui.element("div").props(f"data-task-id={queued_task_id}").classes("cb-composer-queue-item"):
                                ui.label(queued_prompt or _tr(t, "ui.web.mobile.stream_prompt", "输入")).classes("cb-composer-queue-text")
                                with ui.row().classes("cb-composer-queue-actions"):
                                    if queued_task_id:
                                        stop_label = _tr(t, "ui.web.mobile.cancel_task", "停止任务")
                                        ui.button(
                                            "",
                                            on_click=lambda task_id=queued_task_id: on_cancel_task(task_id),
                                            icon="close",
                                            color=None,
                                        ).props(f'flat dense round title="{stop_label}" aria-label="{stop_label}" data-task-id={queued_task_id}').classes("cb-composer-queue-cancel")
                with ui.element("div").classes("cb-composer-upload-panel cb-composer-upload-panel-hidden"):
                    ui.label(_tr(t, "ui.web.mobile.add_image_title", "添加图片附件")).classes("cb-composer-upload-title")
                    ui.upload(
                        multiple=True,
                        max_files=8,
                        max_file_size=12 * 1024 * 1024,
                        auto_upload=True,
                        label=_tr(t, "ui.web.mobile.add_image_upload", "选择图片"),
                        on_upload=lambda event, session=active_session: on_upload_image(session, event),
                    ).props("accept=image/* flat bordered").classes("w-full cb-composer-upload")
                if pending_image_attachments:
                    with ui.element("div").classes("cb-composer-attachment-tray"):
                        for attachment in pending_image_attachments:
                            image_path = str(attachment.get("path") or "")
                            image_source = str(attachment.get("source") or "")
                            image_label = str(attachment.get("label") or _tr(t, "ui.web.mobile.stream_image_attachment", "图片"))
                            with ui.element("div").classes("cb-composer-attachment-pill"):
                                if image_source:
                                    ui.image(image_source).classes("cb-composer-attachment-thumb")
                                ui.label(image_label).classes("cb-composer-attachment-name")
                                ui.button(
                                    "",
                                    on_click=lambda path=image_path, session=active_session: on_remove_image(session, path),
                                    icon="close",
                                    color=None,
                                ).props("flat dense round").classes("cb-composer-attachment-remove")
                message_input = ui.textarea(
                    placeholder=_tr(t, "ui.web.mobile.stream_composer_placeholder", "输入要发给当前会话的内容"),
                ).props("autogrow borderless").classes("w-full cb-composer-input")
                with ui.row().classes("w-full justify-between items-center gap-2 cb-composer-actions"):
                    add_label = _tr(t, "ui.web.mobile.add_image_title", "添加图片附件")
                    ui.button("", icon="add").props(f'flat dense round title="{add_label}" aria-label="{add_label}"').classes("cb-composer-tool-button cb-composer-upload-button").on(
                        "click",
                        js_handler="""
                        () => {
                            const panel = document.querySelector('.cb-composer-upload-panel');
                            const button = document.querySelector('.cb-composer-upload-button');
                            if (!panel || !button) return;
                            const hidden = panel.classList.toggle('cb-composer-upload-panel-hidden');
                            window.__cbComposerUploadOpen = !hidden;
                            const icon = button.querySelector('.q-icon, i');
                            if (icon) icon.textContent = hidden ? 'add' : 'close';
                            window.requestAnimationFrame(() => {
                                const composerZone = document.querySelector('.cb-composer-zone');
                                const height = composerZone ? Math.ceil(composerZone.getBoundingClientRect().height) : 0;
                                document.documentElement.style.setProperty('--cb-composer-height', `${height}px`);
                            });
                        }
                        """,
                    )
                    with ui.row().classes("items-center gap-2 cb-composer-right-actions"):
                        if context_left_percent is not None:
                            meter_label = _tr(t, "ui.web.mobile.context_left", "上下文 {percent}%", percent=context_left_percent)
                            with ui.element("div").props(f"data-context-left={context_left_percent}").classes("cb-context-meter"):
                                with ui.element("span").classes("cb-context-meter-track"):
                                    ui.element("span").classes("cb-context-meter-fill").style(f"width: {context_left_percent}%")
                                ui.label(meter_label).classes("cb-context-meter-label")
                        if latest_active_task_id:
                            stop_label = _tr(t, "ui.web.mobile.cancel_task", "停止任务")
                            ui.button(
                                "",
                                on_click=lambda task_id=latest_active_task_id: on_cancel_task(task_id),
                                icon="stop",
                            ).props(f'unelevated round color=negative title="{stop_label}" aria-label="{stop_label}" data-task-id={latest_active_task_id}').classes("cb-composer-stop-button cb-composer-cancel-button")
                        send_label = (
                            _tr(t, "ui.web.mobile.queue_message", "排队发送")
                            if latest_active_task_id
                            else _tr(t, "ui.web.mobile.send_message", "发送消息")
                        )
                        send_icon = "keyboard_return" if latest_active_task_id else "arrow_upward"
                        ui.button(
                            "",
                            on_click=lambda input_box=message_input, session=active_session, agent=default_agent, backend=default_backend: submit_composer(input_box, session, agent, backend),
                            icon=send_icon,
                            color=None,
                        ).props(f'unelevated round title="{send_label}" aria-label="{send_label}" data-composer-mode=send {"disable" if not active_session else ""}').classes("cb-composer-send-button cb-composer-send-disabled")

def render_mobile_stream_section(
    ui: UIFactoryLike,
    t: Translator,
    mobile_state: dict[str, object],
    selected_session_name: str,
    pending_image_attachments: list[dict[str, str]],
    on_select_session,
    on_send_message,
    on_copy_text,
    on_cancel_task,
    on_load_older,
    on_upload_image,
    on_remove_image,
    on_new_session=None,
) -> None:
    context = _prepare_stream_render_context(mobile_state, selected_session_name)
    active_session = str(context.get("active_session") or "default")
    render_mobile_stream_shell(
        ui,
        active_session,
        lambda: _render_mobile_stream_messages(ui, t, context, on_copy_text, on_cancel_task, on_load_older),
        lambda: _render_mobile_stream_composer(
            ui,
            t,
            context,
            pending_image_attachments,
            on_send_message,
            on_cancel_task,
            on_upload_image,
            on_remove_image,
        ),
    )


def render_diagnostics_section(
    ui: UIFactoryLike,
    model: WebConsoleViewModel,
    t: Translator,
    on_refresh_checks,
    on_refresh_logs,
    on_refresh_external_agents,
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
                with ui.row().classes("items-center justify-between gap-2"):
                    _render_card_title(ui, _tr(t, "ui.web.diagnostics.checks", "环境检查"))
                    ui.button(_tr(t, "ui.web.action.refresh_checks", "刷新检查"), on_click=on_refresh_checks, icon="refresh").props("outline")
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
                with ui.row().classes("items-center justify-between gap-2"):
                    _render_card_title(ui, _tr(t, "ui.card.activity", "运行日志"))
                    log_action_key = "ui.web.action.refresh_logs" if model.logs_loaded else "ui.web.action.load_logs"
                    log_action_fallback = "刷新日志" if model.logs_loaded else "加载日志"
                    ui.button(_tr(t, log_action_key, log_action_fallback), on_click=on_refresh_logs, icon="refresh").props("outline")
                if not model.logs_loaded:
                    ui.label(_tr(t, "ui.web.logs.lazy_list", "点击“加载日志”后再读取并渲染运行日志。")).classes("text-sm cb-muted")
                else:
                    for title, content in model.log_sections:
                        ui.label(title).classes("font-semibold cb-ink")
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

            with ui.dialog() as delete_dialog, _dialog_card(ui):
                ui.label(_tr(t, "ui.web.agents.delete_title", "确认删除 Agent")).classes("text-lg font-semibold")
                ui.label(_tr(t, "ui.web.agents.delete_body", "删除后会移除该 Agent 配置和任务记录。若该 Agent 正被微信桥使用，Hub 会拒绝删除。")).classes("text-sm cb-muted")
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
            with ui.row().classes("items-center justify-between gap-2"):
                _render_card_title(ui, _tr(t, "ui.web.external.title", "外部终端 Agent 进程"))
                ui.button(_tr(t, "ui.web.action.refresh_external_agents", "刷新外部进程"), on_click=on_refresh_external_agents, icon="refresh").props("outline")
            if model.external_agent_processes:
                for item in model.external_agent_processes:
                    with _panel(ui):
                        with ui.dialog() as terminate_dialog, _dialog_card(ui):
                            ui.label(_tr(t, "ui.web.external.terminate_title", "确认结束外部 Agent 进程")).classes("text-lg font-semibold")
                            ui.label(_tr(t, "ui.web.external.terminate_body", "PID {pid} 将被直接结束。这个操作只影响外部终端里手动启动的 Agent 进程。", pid=item.pid)).classes("text-sm cb-muted")
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
                        ui.label(_tr(t, "ui.web.external.process_name", "进程名: {name}", name=item.name)).classes("text-sm cb-ink")
                        if item.session_hint:
                            ui.label(_tr(t, "ui.web.external.session_hint", "会话标识: {hint}", hint=item.session_hint)).classes("text-sm cb-ink")
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
