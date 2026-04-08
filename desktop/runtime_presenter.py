from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QListWidget, QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem

from core.sessions import build_session_rows
from core.view_models import build_diagnostics_view_model, build_issue_panel_view_model, build_session_detail_view_model


@dataclass
class AgentWidgets:
    table: QTableWidget
    detail_text: QPlainTextEdit
    conversation_text: QPlainTextEdit
    session_list: QListWidget


@dataclass
class IssueWidgets:
    summary_label: QLabel
    detail_text: QPlainTextEdit
    repair_button: QPushButton
    manage_accounts_button: QPushButton
    login_button: QPushButton
    cleanup_button: QPushButton
    open_dir_button: QPushButton


def render_agent_table(
    widgets: AgentWidgets,
    hub_state: dict[str, Any],
    session_dir: Path,
    previous_session_name: str,
    render_agent_detail: Callable[[], None],
) -> None:
    session_rows = build_session_rows(hub_state, session_dir)
    widgets.table.setRowCount(len(session_rows))
    for row, session in enumerate(session_rows):
        values = [
            session.name,
            session.status,
            str(session.queue_size),
            str(session.success_count),
            str(session.failure_count),
        ]
        for col, value in enumerate(values):
            item = QTableWidgetItem(value)
            if col == 0:
                item.setData(Qt.UserRole, session.name)
            widgets.table.setItem(row, col, item)
    widgets.table.resizeColumnsToContents()

    if session_rows:
        target_row = 0
        if previous_session_name:
            for row in range(widgets.table.rowCount()):
                item = widgets.table.item(row, 0)
                if item and item.data(Qt.UserRole) == previous_session_name:
                    target_row = row
                    break
        widgets.table.selectRow(target_row)
        render_agent_detail()
        return

    widgets.detail_text.setPlainText("当前没有可显示的会话。")
    widgets.conversation_text.setPlainText("当前没有可显示的会话预览。")


def render_agent_detail(
    widgets: AgentWidgets,
    hub_state: dict[str, Any],
    session_dir: Path,
    session_name: str,
    task_status_text: Callable[[str], str],
    t: Callable[..., str],
) -> None:
    widgets.session_list.blockSignals(True)
    widgets.session_list.clear()
    widgets.session_list.blockSignals(False)

    detail = build_session_detail_view_model(hub_state, session_dir, session_name, task_status_text, t)
    widgets.detail_text.setPlainText(detail.detail_text)
    widgets.conversation_text.setPlainText(detail.conversation_text)


def render_issue_panel(
    widgets: IssueWidgets,
    snapshot: Any,
    bridge_state: dict[str, Any],
    checks: dict[str, Any],
    t: Callable[..., str],
) -> None:
    model = build_issue_panel_view_model(snapshot, bridge_state, checks, t)
    widgets.summary_label.setText(model.summary_text)
    widgets.detail_text.setPlainText(model.detail_text)
    widgets.repair_button.setVisible(model.show_repair_button)
    widgets.manage_accounts_button.setVisible(model.show_manage_accounts_button)
    widgets.login_button.setVisible(model.show_login_button)
    widgets.cleanup_button.setVisible(model.show_cleanup_button)
    widgets.open_dir_button.setVisible(model.show_open_dir_button)


def render_diagnostics_text(checks: dict[str, Any], diag_at: str, t: Callable[..., str]) -> tuple[str, str]:
    model = build_diagnostics_view_model(checks, diag_at, t)
    return model.label_text, model.detail_text
