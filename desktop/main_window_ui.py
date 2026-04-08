from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.action_defs import AUTO_REFRESH_ON_ACTION, DIAGNOSTICS_ACTION, LOGIN_ACTION, REFRESH_ACTION, SESSIONS_ACTION
from core.navigation import DIAGNOSTICS_PAGE, HOME_PAGE, ISSUES_PAGE, SESSIONS_PAGE
from core.shell_schema import APP_SHELL


class Card(QFrame):
    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("cardTitle")
        layout.addWidget(self.title_label)

        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        layout.addLayout(self.body)


def readonly_text() -> QPlainTextEdit:
    widget = QPlainTextEdit()
    widget.setReadOnly(True)
    widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
    return widget


@dataclass
class MainWindowUI:
    summary_label: QLabel
    next_step_label: QLabel
    stack_badge: QLabel
    primary_button: QPushButton
    refresh_button: QPushButton
    scan_login_button: QPushButton
    agent_page_button: QPushButton
    logs_button: QPushButton
    auto_refresh_button: QPushButton
    main_tabs: QTabWidget
    quickstart_card: Card
    quickstart_status: QLabel
    quickstart_steps: QPlainTextEdit
    system_card: Card
    overview_text: QPlainTextEdit
    issue_card: Card
    issue_summary_label: QLabel
    issue_text: QPlainTextEdit
    issue_repair_button: QPushButton
    issue_manage_accounts_button: QPushButton
    issue_login_button: QPushButton
    issue_cleanup_button: QPushButton
    issue_open_dir_button: QPushButton
    agent_card: Card
    agent_table: QTableWidget
    agent_detail_text: QPlainTextEdit
    agent_conversation_text: QPlainTextEdit
    agent_session_list: QListWidget
    diag_card: Card
    diag_time_label: QLabel
    diag_text: QPlainTextEdit
    activity_card: Card
    activity_text: QPlainTextEdit


def build_main_window_ui(
    window: QMainWindow,
    run_primary_action: Callable[[], None],
    refresh_diagnostics: Callable[[], None],
    configure_accounts: Callable[[], None],
    open_sessions_page: Callable[[], None],
    open_logs_page: Callable[[], None],
    toggle_auto_refresh: Callable[[], None],
    repair_environment: Callable[[], None],
    open_accounts_dir: Callable[[], None],
    confirm_emergency_stop: Callable[[], None],
    open_project_dir: Callable[[], None],
    on_agent_selection_changed: Callable[[], None],
    on_agent_session_changed: Callable[[], None],
) -> MainWindowUI:
    root = QWidget()
    window.setCentralWidget(root)

    layout = QVBoxLayout(root)
    layout.setContentsMargins(18, 18, 18, 18)
    layout.setSpacing(14)

    header = QHBoxLayout()
    header.setSpacing(12)
    layout.addLayout(header)

    title_box = QVBoxLayout()
    title_box.setSpacing(4)
    header.addLayout(title_box, 1)

    title = QLabel(APP_SHELL.app_name)
    title_font = QFont("Segoe UI", 20)
    title_font.setBold(True)
    title.setFont(title_font)
    title_box.addWidget(title)

    summary_label = QLabel("正在检测当前状态...")
    summary_label.setWordWrap(True)
    title_box.addWidget(summary_label)

    next_step_label = QLabel(APP_SHELL.app_subtitle)
    next_step_label.setObjectName("subtle")
    next_step_label.setWordWrap(True)
    title_box.addWidget(next_step_label)

    stack_badge = QLabel("检测中")
    stack_badge.setObjectName("statusBadge")
    stack_badge.setAlignment(Qt.AlignCenter)
    stack_badge.setMinimumWidth(170)
    header.addWidget(stack_badge, 0, Qt.AlignTop)

    actions = QHBoxLayout()
    actions.setSpacing(10)
    layout.addLayout(actions)

    primary_button = QPushButton("检测中")
    primary_button.setObjectName("primaryButton")
    primary_button.clicked.connect(run_primary_action)
    primary_button.setMinimumHeight(42)
    actions.addWidget(primary_button, 2)

    refresh_button = QPushButton(REFRESH_ACTION.label)
    refresh_button.clicked.connect(refresh_diagnostics)
    actions.addWidget(refresh_button)

    scan_login_button = QPushButton(LOGIN_ACTION.label)
    scan_login_button.clicked.connect(configure_accounts)
    actions.addWidget(scan_login_button)

    main_tabs = QTabWidget()
    layout.addWidget(main_tabs, 10)

    agent_page_button = QPushButton(SESSIONS_ACTION.label)
    agent_page_button.clicked.connect(open_sessions_page)
    actions.addWidget(agent_page_button)

    logs_button = QPushButton(DIAGNOSTICS_ACTION.label)
    logs_button.clicked.connect(open_logs_page)
    actions.addWidget(logs_button)

    auto_refresh_button = QPushButton(AUTO_REFRESH_ON_ACTION.label)
    auto_refresh_button.setCheckable(True)
    auto_refresh_button.setChecked(True)
    auto_refresh_button.clicked.connect(toggle_auto_refresh)
    actions.addWidget(auto_refresh_button)

    home_page = QWidget()
    home_layout = QVBoxLayout(home_page)
    home_layout.setContentsMargins(0, 0, 0, 0)
    home_layout.setSpacing(14)
    main_tabs.addTab(home_page, HOME_PAGE.title)

    top_splitter = QSplitter(Qt.Horizontal)
    top_splitter.setChildrenCollapsible(False)
    home_layout.addWidget(top_splitter, 1)

    quickstart_card = Card("快速使用")
    top_splitter.addWidget(quickstart_card)
    quickstart_status = QLabel("正在分析下一步...")
    quickstart_status.setWordWrap(True)
    quickstart_card.body.addWidget(quickstart_status)
    quickstart_steps = readonly_text()
    quickstart_steps.setMaximumBlockCount(20)
    quickstart_steps.setMinimumHeight(180)
    quickstart_card.body.addWidget(quickstart_steps)

    system_card = Card("系统结论")
    top_splitter.addWidget(system_card)
    overview_text = readonly_text()
    overview_text.setMinimumHeight(180)
    system_card.body.addWidget(overview_text)
    top_splitter.setSizes([420, 560])

    issues_page = QWidget()
    issues_layout = QVBoxLayout(issues_page)
    issues_layout.setContentsMargins(0, 0, 0, 0)
    issues_layout.setSpacing(14)
    main_tabs.addTab(issues_page, ISSUES_PAGE.title)

    issue_card = Card("当前异常")
    issues_layout.addWidget(issue_card, 1)
    issue_summary_label = QLabel("正在分析当前异常...")
    issue_summary_label.setWordWrap(True)
    issue_card.body.addWidget(issue_summary_label)
    issue_text = readonly_text()
    issue_text.setMaximumBlockCount(80)
    issue_text.setMinimumHeight(360)
    issue_card.body.addWidget(issue_text)

    issue_actions = QHBoxLayout()
    issue_actions.setSpacing(10)
    issue_card.body.addLayout(issue_actions)

    issue_repair_button = QPushButton("处理依赖")
    issue_repair_button.clicked.connect(repair_environment)
    issue_actions.addWidget(issue_repair_button)

    issue_manage_accounts_button = QPushButton("管理微信账号")
    issue_manage_accounts_button.clicked.connect(configure_accounts)
    issue_actions.addWidget(issue_manage_accounts_button)

    issue_login_button = QPushButton("打开微信账号目录")
    issue_login_button.clicked.connect(open_accounts_dir)
    issue_actions.addWidget(issue_login_button)

    issue_cleanup_button = QPushButton("清理残留进程")
    issue_cleanup_button.clicked.connect(confirm_emergency_stop)
    issue_actions.addWidget(issue_cleanup_button)

    issue_open_dir_button = QPushButton("打开程序目录")
    issue_open_dir_button.clicked.connect(open_project_dir)
    issue_actions.addWidget(issue_open_dir_button)

    agent_page = QWidget()
    agent_layout = QVBoxLayout(agent_page)
    agent_layout.setContentsMargins(0, 0, 0, 0)
    agent_layout.setSpacing(14)
    main_tabs.addTab(agent_page, SESSIONS_PAGE.title)

    agent_card = Card("会话")
    agent_layout.addWidget(agent_card, 1)
    agent_splitter = QSplitter(Qt.Vertical)
    agent_splitter.setChildrenCollapsible(False)
    agent_card.body.addWidget(agent_splitter)

    agent_table = QTableWidget(0, 5)
    agent_table.setHorizontalHeaderLabels(["会话", "状态", "队列", "成功", "失败"])
    agent_table.verticalHeader().setVisible(False)
    agent_table.setEditTriggers(QTableWidget.NoEditTriggers)
    agent_table.setSelectionMode(QTableWidget.SingleSelection)
    agent_table.setSelectionBehavior(QTableWidget.SelectRows)
    agent_table.horizontalHeader().setStretchLastSection(True)
    agent_table.setAlternatingRowColors(True)
    agent_table.setMinimumHeight(220)
    agent_table.itemSelectionChanged.connect(on_agent_selection_changed)
    agent_splitter.addWidget(agent_table)

    agent_detail_text = readonly_text()
    agent_detail_text.setMinimumHeight(180)
    agent_detail_text.setPlainText("选择一个会话后，这里会显示状态、会话文件和任务摘要。")
    agent_conversation_text = readonly_text()
    agent_conversation_text.setMinimumHeight(180)
    agent_conversation_text.setPlainText("选择一个会话后，这里会显示最近几轮对话预览。")
    detail_container = QWidget()
    detail_layout = QVBoxLayout(detail_container)
    detail_layout.setContentsMargins(0, 0, 0, 0)
    detail_layout.setSpacing(8)
    session_header = QLabel("会话文件")
    session_header.setObjectName("subtle")
    detail_layout.addWidget(session_header)
    agent_session_list = QListWidget()
    agent_session_list.setAlternatingRowColors(True)
    agent_session_list.setMinimumHeight(120)
    agent_session_list.itemSelectionChanged.connect(on_agent_session_changed)
    detail_layout.addWidget(agent_session_list)
    session_header.setVisible(False)
    agent_session_list.setVisible(False)
    detail_splitter = QSplitter(Qt.Horizontal)
    detail_splitter.setChildrenCollapsible(False)
    detail_splitter.addWidget(agent_detail_text)
    detail_splitter.addWidget(agent_conversation_text)
    detail_splitter.setSizes([320, 360])
    detail_layout.addWidget(detail_splitter)
    agent_splitter.addWidget(detail_container)
    agent_splitter.setSizes([260, 420])

    diagnostics_page = QWidget()
    diagnostics_layout = QVBoxLayout(diagnostics_page)
    diagnostics_layout.setContentsMargins(0, 0, 0, 0)
    diagnostics_layout.setSpacing(14)
    main_tabs.addTab(diagnostics_page, DIAGNOSTICS_PAGE.title)

    bottom_splitter = QSplitter(Qt.Horizontal)
    bottom_splitter.setChildrenCollapsible(False)
    diagnostics_layout.addWidget(bottom_splitter, 1)

    diag_card = Card("自动诊断")
    bottom_splitter.addWidget(diag_card)
    diag_time_label = QLabel("环境检查: 尚未完成")
    diag_time_label.setObjectName("subtle")
    diag_card.body.addWidget(diag_time_label)
    diag_text = readonly_text()
    diag_text.setMinimumHeight(220)
    diag_card.body.addWidget(diag_text)

    activity_card = Card("运行日志")
    bottom_splitter.addWidget(activity_card)
    activity_text = readonly_text()
    activity_text.setPlainText("启动后会在这里显示操作记录。")
    activity_text.setMinimumHeight(220)
    activity_card.body.addWidget(activity_text)
    bottom_splitter.setSizes([520, 520])

    return MainWindowUI(
        summary_label=summary_label,
        next_step_label=next_step_label,
        stack_badge=stack_badge,
        primary_button=primary_button,
        refresh_button=refresh_button,
        scan_login_button=scan_login_button,
        agent_page_button=agent_page_button,
        logs_button=logs_button,
        auto_refresh_button=auto_refresh_button,
        main_tabs=main_tabs,
        quickstart_card=quickstart_card,
        quickstart_status=quickstart_status,
        quickstart_steps=quickstart_steps,
        system_card=system_card,
        overview_text=overview_text,
        issue_card=issue_card,
        issue_summary_label=issue_summary_label,
        issue_text=issue_text,
        issue_repair_button=issue_repair_button,
        issue_manage_accounts_button=issue_manage_accounts_button,
        issue_login_button=issue_login_button,
        issue_cleanup_button=issue_cleanup_button,
        issue_open_dir_button=issue_open_dir_button,
        agent_card=agent_card,
        agent_table=agent_table,
        agent_detail_text=agent_detail_text,
        agent_conversation_text=agent_conversation_text,
        agent_session_list=agent_session_list,
        diag_card=diag_card,
        diag_time_label=diag_time_label,
        diag_text=diag_text,
        activity_card=activity_card,
        activity_text=activity_text,
    )
