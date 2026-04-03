from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def running_under_debugger() -> bool:
    return bool(sys.gettrace()) or bool(os.environ.get("PYCHARM_HOSTED"))


def ensure_desktop_dependencies() -> None:
    required = ("PySide6", "psutil", "qrcode")
    missing = [name for name in required if importlib.util.find_spec(name) is None]
    if not missing:
        return

    print(f"Installing missing desktop dependencies: {', '.join(missing)}", file=sys.stderr, flush=True)
    completed = subprocess.run(
        [sys.executable, "-m", "pip", "install", *missing],
        cwd=str(APP_DIR),
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=0 if running_under_debugger() else CREATE_NO_WINDOW,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(f"Failed to install desktop dependencies: {', '.join(missing)}")


ensure_desktop_dependencies()

try:
    from PySide6.QtCore import QTimer, Qt, Signal, QUrl
    from PySide6.QtGui import QDesktopServices, QFont, QIcon, QPixmap
    from PySide6.QtWidgets import (
        QApplication,
        QDialog,
        QFrame,
        QHBoxLayout,
        QInputDialog,
        QLabel,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QPlainTextEdit,
        QSizePolicy,
        QSplitter,
        QTableWidget,
        QTableWidgetItem,
        QTabWidget,
        QVBoxLayout,
        QWidget,
    )
except ImportError as exc:
    raise SystemExit(
        "Missing desktop dependencies. Run install-dependencies.cmd first, or install PySide6 and psutil manually."
    ) from exc

import base64
import io
import json
import urllib.parse
import urllib.request

from bridge_config import BridgeConfig
from codex_wechat_bootstrap import build_nvm_node_command, collect_checks, run_shell_command
from codex_wechat_runtime import (
    BRIDGE_STATE_PATH,
    HUB_STATE_PATH,
    LOG_DIR,
    emergency_stop,
    ensure_runtime_dirs,
    get_runtime_snapshot,
    read_json,
    start_all,
    stop_all,
)
from localization import Localizer


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


class MainWindow(QMainWindow):
    diagnostics_ready = Signal(dict, str)
    activity_logged = Signal(str)
    runtime_refresh_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        ensure_runtime_dirs()
        self.localizer = Localizer()
        self.setWindowTitle("ChatBridge")
        self.setMinimumSize(1100, 760)
        icon_path = APP_DIR / "codex_wechat_desktop.ico"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self.checks: dict[str, object] = {}
        self.last_diag_at = "尚未完成"
        self.primary_action = "manual"
        self._diagnostics_running = False
        self._auto_repair_prompted = False
        self._auto_refresh_enabled = True
        self._last_hub_signature: tuple | None = None
        self._issue_panel_expanded = False

        self.diagnostics_ready.connect(self._apply_diagnostics)
        self.activity_logged.connect(self._append_activity)
        self.runtime_refresh_requested.connect(self.refresh_runtime)

        self._build_ui()
        self._apply_localized_static_texts()
        self.refresh_runtime()
        self.refresh_diagnostics()

        self.runtime_timer = QTimer(self)
        self.runtime_timer.timeout.connect(self.refresh_runtime)
        self.runtime_timer.start(8000)

        self.diag_timer = QTimer(self)
        self.diag_timer.timeout.connect(self.refresh_diagnostics)
        self.diag_timer.start(120000)

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)

        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(12)
        layout.addLayout(header)

        title_box = QVBoxLayout()
        title_box.setSpacing(4)
        header.addLayout(title_box, 1)

        title = QLabel("ChatBridge")
        title_font = QFont("Segoe UI", 20)
        title_font.setBold(True)
        title.setFont(title_font)
        title_box.addWidget(title)

        self.summary_label = QLabel("正在检测当前状态...")
        self.summary_label.setWordWrap(True)
        title_box.addWidget(self.summary_label)

        self.next_step_label = QLabel("浮浮酱会自动检测环境、微信登录状态和后台进程。")
        self.next_step_label.setObjectName("subtle")
        self.next_step_label.setWordWrap(True)
        title_box.addWidget(self.next_step_label)

        self.stack_badge = QLabel("检测中")
        self.stack_badge.setObjectName("statusBadge")
        self.stack_badge.setAlignment(Qt.AlignCenter)
        self.stack_badge.setMinimumWidth(170)
        header.addWidget(self.stack_badge, 0, Qt.AlignTop)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        layout.addLayout(actions)

        self.primary_button = QPushButton("检测中")
        self.primary_button.setObjectName("primaryButton")
        self.primary_button.clicked.connect(self.run_primary_action)
        self.primary_button.setMinimumHeight(42)
        actions.addWidget(self.primary_button, 2)

        self.refresh_button = QPushButton("重新检测")
        self.refresh_button.clicked.connect(self.refresh_diagnostics)
        actions.addWidget(self.refresh_button)

        self.scan_login_button = QPushButton("扫码登录微信")
        self.scan_login_button.clicked.connect(self.configure_accounts)
        actions.addWidget(self.scan_login_button)

        self.agent_page_button = QPushButton("查看会话")
        self.agent_page_button.clicked.connect(lambda: self.main_tabs.setCurrentIndex(2))
        actions.addWidget(self.agent_page_button)

        self.logs_button = QPushButton("诊断与日志")
        self.logs_button.clicked.connect(lambda: self.main_tabs.setCurrentIndex(3))
        actions.addWidget(self.logs_button)

        self.auto_refresh_button = QPushButton("自动刷新：开")
        self.auto_refresh_button.setCheckable(True)
        self.auto_refresh_button.setChecked(True)
        self.auto_refresh_button.clicked.connect(self.toggle_auto_refresh)
        actions.addWidget(self.auto_refresh_button)

        self.main_tabs = QTabWidget()
        layout.addWidget(self.main_tabs, 10)

        home_page = QWidget()
        home_layout = QVBoxLayout(home_page)
        home_layout.setContentsMargins(0, 0, 0, 0)
        home_layout.setSpacing(14)
        self.main_tabs.addTab(home_page, "首页")

        top_splitter = QSplitter(Qt.Horizontal)
        top_splitter.setChildrenCollapsible(False)
        home_layout.addWidget(top_splitter, 1)

        self.quickstart_card = Card("快速使用")
        top_splitter.addWidget(self.quickstart_card)
        self.quickstart_status = QLabel("正在分析下一步...")
        self.quickstart_status.setWordWrap(True)
        self.quickstart_card.body.addWidget(self.quickstart_status)
        self.quickstart_steps = self._readonly_text()
        self.quickstart_steps.setMaximumBlockCount(20)
        self.quickstart_steps.setMinimumHeight(180)
        self.quickstart_card.body.addWidget(self.quickstart_steps)

        self.system_card = Card("系统结论")
        top_splitter.addWidget(self.system_card)
        self.overview_text = self._readonly_text()
        self.overview_text.setMinimumHeight(180)
        self.system_card.body.addWidget(self.overview_text)

        top_splitter.setSizes([420, 560])

        issues_page = QWidget()
        issues_layout = QVBoxLayout(issues_page)
        issues_layout.setContentsMargins(0, 0, 0, 0)
        issues_layout.setSpacing(14)
        self.main_tabs.addTab(issues_page, "异常")

        self.issue_card = Card("当前异常")
        issues_layout.addWidget(self.issue_card, 1)
        self.issue_summary_label = QLabel("正在分析当前异常...")
        self.issue_summary_label.setWordWrap(True)
        self.issue_card.body.addWidget(self.issue_summary_label)
        self.issue_text = self._readonly_text()
        self.issue_text.setMaximumBlockCount(80)
        self.issue_text.setMinimumHeight(360)
        self.issue_card.body.addWidget(self.issue_text)

        issue_actions = QHBoxLayout()
        issue_actions.setSpacing(10)
        self.issue_card.body.addLayout(issue_actions)

        self.issue_repair_button = QPushButton("处理依赖")
        self.issue_repair_button.clicked.connect(self.repair_environment)
        issue_actions.addWidget(self.issue_repair_button)

        self.issue_manage_accounts_button = QPushButton("管理微信账号")
        self.issue_manage_accounts_button.clicked.connect(self.configure_accounts)
        issue_actions.addWidget(self.issue_manage_accounts_button)

        self.issue_login_button = QPushButton("打开微信账号目录")
        self.issue_login_button.clicked.connect(lambda: self.open_path(APP_DIR / "accounts"))
        issue_actions.addWidget(self.issue_login_button)

        self.issue_cleanup_button = QPushButton("清理残留进程")
        self.issue_cleanup_button.clicked.connect(self.confirm_emergency_stop)
        issue_actions.addWidget(self.issue_cleanup_button)

        self.issue_open_dir_button = QPushButton("打开程序目录")
        self.issue_open_dir_button.clicked.connect(lambda: self.open_path(APP_DIR))
        issue_actions.addWidget(self.issue_open_dir_button)

        self.issue_action_buttons = [
            self.issue_repair_button,
            self.issue_login_button,
            self.issue_cleanup_button,
            self.issue_open_dir_button,
        ]

        agent_page = QWidget()
        agent_layout = QVBoxLayout(agent_page)
        agent_layout.setContentsMargins(0, 0, 0, 0)
        agent_layout.setSpacing(14)
        self.main_tabs.addTab(agent_page, "会话")

        self.agent_card = Card("会话")
        agent_layout.addWidget(self.agent_card, 1)
        agent_splitter = QSplitter(Qt.Vertical)
        agent_splitter.setChildrenCollapsible(False)
        self.agent_card.body.addWidget(agent_splitter)

        self.agent_table = QTableWidget(0, 5)
        self.agent_table.setHorizontalHeaderLabels(["会话", "状态", "队列", "成功", "失败"])
        self.agent_table.verticalHeader().setVisible(False)
        self.agent_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.agent_table.setSelectionMode(QTableWidget.SingleSelection)
        self.agent_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.agent_table.horizontalHeader().setStretchLastSection(True)
        self.agent_table.setAlternatingRowColors(True)
        self.agent_table.setMinimumHeight(220)
        self.agent_table.itemSelectionChanged.connect(self._on_agent_selection_changed)
        agent_splitter.addWidget(self.agent_table)

        self.agent_detail_text = self._readonly_text()
        self.agent_detail_text.setMinimumHeight(180)
        self.agent_detail_text.setPlainText("选择一个会话后，这里会显示状态、会话文件和任务摘要。")
        self.agent_conversation_text = self._readonly_text()
        self.agent_conversation_text.setMinimumHeight(180)
        self.agent_conversation_text.setPlainText("选择一个会话后，这里会显示最近几轮对话预览。")
        detail_container = QWidget()
        detail_layout = QVBoxLayout(detail_container)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(8)
        session_header = QLabel("会话文件")
        session_header.setObjectName("subtle")
        detail_layout.addWidget(session_header)
        self.agent_session_list = QListWidget()
        self.agent_session_list.setAlternatingRowColors(True)
        self.agent_session_list.setMinimumHeight(120)
        self.agent_session_list.itemSelectionChanged.connect(self._on_agent_session_changed)
        detail_layout.addWidget(self.agent_session_list)
        session_header.setVisible(False)
        self.agent_session_list.setVisible(False)
        detail_splitter = QSplitter(Qt.Horizontal)
        detail_splitter.setChildrenCollapsible(False)
        detail_splitter.addWidget(self.agent_detail_text)
        detail_splitter.addWidget(self.agent_conversation_text)
        detail_splitter.setSizes([320, 360])
        detail_layout.addWidget(detail_splitter)
        agent_splitter.addWidget(detail_container)
        agent_splitter.setSizes([260, 420])

        diagnostics_page = QWidget()
        diagnostics_layout = QVBoxLayout(diagnostics_page)
        diagnostics_layout.setContentsMargins(0, 0, 0, 0)
        diagnostics_layout.setSpacing(14)
        self.main_tabs.addTab(diagnostics_page, "诊断与日志")

        bottom_splitter = QSplitter(Qt.Horizontal)
        bottom_splitter.setChildrenCollapsible(False)
        diagnostics_layout.addWidget(bottom_splitter, 1)

        self.diag_card = Card("自动诊断")
        bottom_splitter.addWidget(self.diag_card)
        self.diag_time_label = QLabel("环境检查: 尚未完成")
        self.diag_time_label.setObjectName("subtle")
        self.diag_card.body.addWidget(self.diag_time_label)
        self.diag_text = self._readonly_text()
        self.diag_text.setMinimumHeight(220)
        self.diag_card.body.addWidget(self.diag_text)

        self.activity_card = Card("运行日志")
        bottom_splitter.addWidget(self.activity_card)
        self.activity_text = self._readonly_text()
        self.activity_text.setPlainText("启动后会在这里显示操作记录。")
        self.activity_text.setMinimumHeight(220)
        self.activity_card.body.addWidget(self.activity_text)
        bottom_splitter.setSizes([520, 520])

        self._apply_styles()

    def _apply_localized_static_texts(self) -> None:
        self.summary_label.setText(self._t("ui.summary.checking"))
        self.next_step_label.setText(self._t("ui.next_step.initial"))
        self.stack_badge.setText(self._t("ui.status.checking"))
        self.primary_button.setText(self._t("ui.status.checking"))
        self.refresh_button.setText(self._t("ui.button.refresh"))
        self.agent_page_button.setText(self._t("ui.button.sessions"))
        self.logs_button.setText(self._t("ui.button.logs"))
        self.auto_refresh_button.setText(self._t("ui.auto_refresh.on"))

        self.main_tabs.setTabText(0, self._t("ui.tab.home"))
        self.main_tabs.setTabText(1, self._t("ui.tab.issues"))
        self.main_tabs.setTabText(2, self._t("ui.tab.sessions"))
        self.main_tabs.setTabText(3, self._t("ui.tab.logs"))

        self.quickstart_card.title_label.setText(self._t("ui.card.quickstart"))
        self.quickstart_status.setText(self._t("ui.quickstart.analyzing"))
        self.system_card.title_label.setText(self._t("ui.card.system"))
        self.issue_card.title_label.setText(self._t("ui.card.issues"))
        self.issue_summary_label.setText(self._t("ui.issues.analyzing"))
        self.issue_repair_button.setText(self._t("ui.button.repair"))
        self.issue_manage_accounts_button.setText(self._t("ui.button.manage_accounts"))
        self.issue_login_button.setText(self._t("ui.button.open_accounts"))
        self.issue_cleanup_button.setText(self._t("ui.button.cleanup"))
        self.issue_open_dir_button.setText(self._t("ui.button.open_project"))
        self.agent_card.title_label.setText(self._t("ui.card.sessions"))
        self.agent_table.setHorizontalHeaderLabels(
            [
                self._t("ui.table.session"),
                self._t("ui.table.status"),
                self._t("ui.table.queue"),
                self._t("ui.table.success"),
                self._t("ui.table.failure"),
            ]
        )
        self.agent_detail_text.setPlainText(self._t("ui.agent.select_session"))
        self.agent_conversation_text.setPlainText(self._t("ui.agent.select_preview"))
        self.diag_card.title_label.setText(self._t("ui.card.diagnostics"))
        self.diag_time_label.setText(self._t("ui.diagnostics.not_completed"))
        self.activity_card.title_label.setText(self._t("ui.card.activity"))
        self.activity_text.setPlainText(self._t("ui.activity.empty"))

    def _readonly_text(self) -> QPlainTextEdit:
        widget = QPlainTextEdit()
        widget.setReadOnly(True)
        widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        return widget

    def _t(self, key: str, **kwargs: object) -> str:
        return self.localizer.translate(key, **kwargs)

    def _task_status_text(self, status: str) -> str:
        return self._t(f"ui.task_status.{status}")

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow {
                background: #f3f1eb;
            }
            QLabel {
                color: #1f2933;
                font-size: 14px;
            }
            QLabel#subtle {
                color: #52606d;
                font-size: 13px;
            }
            QLabel#statusBadge {
                background: #fff7d6;
                color: #8d5d00;
                border: 1px solid #eadcb0;
                border-radius: 16px;
                padding: 10px 12px;
                font-size: 13px;
                font-weight: 700;
            }
            QFrame#card {
                background: #fffdf8;
                border: 1px solid #ded7ca;
                border-radius: 18px;
            }
            QLabel#cardTitle {
                font-size: 16px;
                font-weight: 700;
                color: #102a43;
            }
            QPushButton {
                background: #ffffff;
                border: 1px solid #d9d2c3;
                border-radius: 12px;
                padding: 10px 14px;
                font-size: 13px;
            }
            QPushButton:hover {
                background: #f9f6ef;
            }
            QPushButton#primaryButton {
                background: #0f766e;
                color: white;
                border: 0;
                font-weight: 700;
            }
            QPushButton#primaryButton:hover {
                background: #0b5f59;
            }
            QPlainTextEdit, QTableWidget {
                background: #fffdfa;
                border: 1px solid #e7e1d5;
                border-radius: 12px;
                font-family: Consolas;
                font-size: 12px;
            }
            QHeaderView::section {
                background: #efe8da;
                border: 0;
                padding: 8px;
                font-weight: 700;
            }
            """
        )

    def refresh_runtime(self) -> None:
        if not self._auto_refresh_enabled and self.sender() is self.runtime_timer:
            return
        snapshot = get_runtime_snapshot()
        hub_state = read_json(HUB_STATE_PATH)
        bridge_state = read_json(BRIDGE_STATE_PATH)

        if snapshot.hub_running and snapshot.bridge_running:
            self.stack_badge.setText("服务运行中")
            self.stack_badge.setStyleSheet("background:#d9f3e4;color:#12633b;border:1px solid #b9dfca;border-radius:16px;padding:10px 12px;font-weight:700;")
        elif snapshot.hub_running or snapshot.bridge_running:
            self.stack_badge.setText("服务部分运行")
            self.stack_badge.setStyleSheet("background:#fff2cc;color:#946200;border:1px solid #eadcb0;border-radius:16px;padding:10px 12px;font-weight:700;")
        else:
            self.stack_badge.setText("服务已停止")
            self.stack_badge.setStyleSheet("background:#f8d7da;color:#8a1c2b;border:1px solid #efbac2;border-radius:16px;padding:10px 12px;font-weight:700;")

        overview_lines = [
            f"Hub: {'运行中' if snapshot.hub_running else '已停止'} {self._pid_text(snapshot.hub_pid)}",
            f"Bridge: {'运行中' if snapshot.bridge_running else '已停止'} {self._pid_text(snapshot.bridge_pid)}",
            f"Codex 进程数: {len(snapshot.codex_processes)}",
            "",
        ]
        if snapshot.codex_processes:
            overview_lines.extend(snapshot.codex_processes[:8])
        else:
            overview_lines.append("没有检测到残留 Codex 进程。")

        if bridge_state:
            overview_lines.extend(["", "微信桥状态:"])
            overview_lines.extend(f"{key}: {value}" for key, value in list(bridge_state.items())[:8])

        self.overview_text.setPlainText("\n".join(overview_lines))
        hub_signature = self._build_hub_signature(hub_state)
        if hub_signature != self._last_hub_signature:
            self._render_agents(hub_state)
            self._last_hub_signature = hub_signature
        self._render_issues(snapshot, bridge_state)
        self._refresh_summary(snapshot)

    def refresh_diagnostics(self) -> None:
        if self._diagnostics_running:
            return
        self._diagnostics_running = True

        def worker() -> None:
            checks = {item.key: item for item in collect_checks(APP_DIR)}
            diag_at = datetime.now().strftime("%H:%M:%S")
            self.diagnostics_ready.emit(checks, diag_at)

        threading.Thread(target=worker, daemon=True).start()

    def _render_agents(self, hub_state: dict) -> None:
        previous_session_name = self._selected_agent_id()
        tasks = hub_state.get("tasks", [])
        session_rows: list[dict[str, str | int]] = []
        session_names: set[str] = {"default"}
        for task in tasks:
            session_names.add(self._normalize_task_session_name(task))
        session_dir = APP_DIR / ".runtime" / "sessions"
        if session_dir.exists():
            for session_file in session_dir.glob("*.txt"):
                session_names.add(self._session_name_from_file("", session_file))

        for session_name in sorted(session_names):
            related_tasks = [task for task in tasks if self._normalize_task_session_name(task) == session_name]
            last_task = related_tasks[0] if related_tasks else {}
            queue_size = sum(1 for task in related_tasks if task.get("status") in {"queued", "running"})
            success_count = sum(1 for task in related_tasks if task.get("status") == "succeeded")
            failure_count = sum(1 for task in related_tasks if task.get("status") == "failed")
            if queue_size:
                status = "running" if any(task.get("status") == "running" for task in related_tasks) else "queued"
            elif related_tasks:
                status = str(last_task.get("status") or "idle")
            else:
                status = "idle"
            session_rows.append(
                {
                    "name": session_name,
                    "status": status,
                    "queue_size": queue_size,
                    "success_count": success_count,
                    "failure_count": failure_count,
                }
            )

        self.agent_table.setRowCount(len(session_rows))
        for row, session in enumerate(session_rows):
            values = [
                str(session["name"]),
                str(session["status"]),
                str(session["queue_size"]),
                str(session["success_count"]),
                str(session["failure_count"]),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 0:
                    item.setData(Qt.UserRole, session["name"])
                self.agent_table.setItem(row, col, item)
        self.agent_table.resizeColumnsToContents()
        if session_rows:
            target_row = 0
            if previous_session_name:
                for row in range(self.agent_table.rowCount()):
                    item = self.agent_table.item(row, 0)
                    if item and item.data(Qt.UserRole) == previous_session_name:
                        target_row = row
                        break
            self.agent_table.selectRow(target_row)
            self._render_agent_detail(hub_state)
        else:
            self.agent_detail_text.setPlainText("当前没有可显示的会话。")
            self.agent_conversation_text.setPlainText("当前没有可显示的会话预览。")

    def _refresh_summary(self, snapshot) -> None:
        missing = [item.label for item in self.checks.values() if not item.ok] if self.checks else []
        if missing:
            self.summary_label.setText(f"当前有 {len(missing)} 项未就绪：{'、'.join(missing[:4])}")
        elif snapshot.hub_running and snapshot.bridge_running:
            self.summary_label.setText("环境已就绪，微信桥和会话后台都在运行。")
        else:
            self.summary_label.setText("环境已就绪，等待启动后台服务。")

        action_key, label, hint = self._decide_primary_action(snapshot)
        self.primary_action = action_key
        self.primary_button.setText(label)
        self.next_step_label.setText(hint)
        self._render_quickstart(snapshot)

    def run_command(self, label: str, command: str) -> None:
        should_run = QMessageBox.question(self, "确认执行", f"{label}\n\n将运行：\n{command}\n\n继续吗？")
        if should_run != QMessageBox.Yes:
            return

        def worker() -> None:
            code, output = run_shell_command(command, APP_DIR)
            self.activity_logged.emit(f"[命令] {label}\n$ {command}\nexit={code}\n{output or '(no output)'}\n")
            self.diagnostics_ready.emit({item.key: item for item in collect_checks(APP_DIR)}, datetime.now().strftime("%H:%M:%S"))

        threading.Thread(target=worker, daemon=True).start()

    def run_async_action(self, label: str, action) -> None:
        def worker() -> None:
            for line in action():
                self.activity_logged.emit(f"[动作] {label}: {line}")
            self.runtime_refresh_requested.emit()

        threading.Thread(target=worker, daemon=True).start()

    def confirm_emergency_stop(self) -> None:
        should_run = QMessageBox.question(
            self,
            "清理残留进程",
            "这会强制清理残留的 Hub、Bridge 和 Codex 相关进程。现在继续吗？",
        )
        if should_run == QMessageBox.Yes:
            self.run_async_action("清理残留进程", emergency_stop)

    def open_path(self, path: Path) -> None:
        if not path.exists():
            QMessageBox.critical(self, "路径不存在", str(path))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def closeEvent(self, event) -> None:  # type: ignore[override]
        snapshot = get_runtime_snapshot()
        if snapshot.hub_running or snapshot.bridge_running:
            should_stop = QMessageBox.question(
                self,
                "退出前停止服务",
                "检测到后台仍在运行。退出前停止 Hub、Bridge 和 Codex 子进程吗？",
            )
            if should_stop == QMessageBox.Yes:
                for line in stop_all():
                    self.activity_logged.emit(f"[退出] {line}")
        event.accept()

    def _append_activity(self, text: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.activity_text.appendPlainText(f"{timestamp} {text}")
        self.activity_text.verticalScrollBar().setValue(self.activity_text.verticalScrollBar().maximum())

    @staticmethod
    def _step_line(title: str, done: bool) -> str:
        prefix = "[完成]" if done else "[待处理]"
        return f"{prefix} {title}"

    def _apply_diagnostics(self, checks: dict, diag_at: str) -> None:
        self._diagnostics_running = False
        self.checks = checks
        self.last_diag_at = diag_at
        self.diag_time_label.setText(f"环境检查: {diag_at}")

        ordered_keys = [
            "python",
            "winget",
            "nvm",
            "pyside6",
            "psutil",
            "node",
            "npm",
            "codex",
            "weixin_account",
            "project_files",
        ]
        lines: list[str] = []
        for key in ordered_keys:
            item = checks.get(key)
            if item is None:
                continue
            status = "OK" if item.ok else "缺失"
            lines.append(f"[{status}] {item.label}: {item.detail}")
        self.diag_text.setPlainText("\n".join(lines))
        self.refresh_runtime()
        self._maybe_prompt_auto_repair()

    def _decide_primary_action(self, snapshot) -> tuple[str, str, str]:
        checks = self.checks
        if snapshot.hub_running or snapshot.bridge_running:
            return "stop", "停止服务", "关闭桌面前优先通过这里正常停止，避免残留后台 Codex 进程。"

        blocking = [key for key in ["python", "project_files"] if checks.get(key) and not checks[key].ok]
        if self._is_missing("nvm") and self._is_missing("winget"):
            blocking.append("nvm")
        if blocking:
            return "manual", "查看诊断", "基础环境本身不完整，先看下方自动诊断，把缺失项补齐。"

        auto_fixable = [key for key in ["pyside6", "psutil", "nvm", "node", "npm", "codex"] if checks.get(key) and not checks[key].ok]
        if auto_fixable:
            return "repair", "一键补齐依赖", "浮浮酱会按顺序补齐桌面依赖、NVM/Node 和 Codex CLI。"

        if checks.get("weixin_account") and not checks["weixin_account"].ok:
            return "login", "打开微信账号目录", "当前缺少项目内微信账号文件，先将 json/sync 文件放到项目目录。"

        return "start", "启动服务", "环境检测已通过，直接启动微信桥和会话后台即可。"

    def run_primary_action(self) -> None:
        if self.primary_action == "stop":
            self.run_async_action("停止整套服务", stop_all)
        elif self.primary_action == "repair":
            self.repair_environment()
        elif self.primary_action == "login":
            self.configure_accounts()
        elif self.primary_action == "start":
            self.run_async_action("启动整套服务", start_all)
        else:
            self.refresh_diagnostics()

    def repair_environment(self) -> None:
        self._auto_repair_prompted = True
        commands: list[tuple[str, str]] = []
        will_have_node = not (self._is_missing("node") or self._is_missing("npm"))
        if self._is_missing("pyside6") or self._is_missing("psutil"):
            commands.append(("安装桌面依赖", "python -m pip install PySide6 psutil"))
        if self._is_missing("nvm") and not self._is_missing("winget"):
            commands.append(
                (
                    "安装 NVM for Windows",
                    "winget install CoreyButler.NVMforWindows --accept-package-agreements --accept-source-agreements",
                )
            )
        if self._is_missing("node") or self._is_missing("npm"):
            commands.append(("通过 NVM 安装 Node 24.14.1", build_nvm_node_command()))
            will_have_node = True
        if self._is_missing("codex") and will_have_node:
            commands.append(("安装 Codex CLI", "npm.cmd install -g codex"))

        if not commands:
            QMessageBox.information(self, "无需修复", "没有可自动修复的依赖项。")
            return

        summary = "\n".join(f"- {label}: {command}" for label, command in commands)
        should_run = QMessageBox.question(self, "确认自动修复", f"将执行这些命令：\n\n{summary}\n\n继续吗？")
        if should_run != QMessageBox.Yes:
            return

        def worker() -> None:
            for label, command in commands:
                code, output = run_shell_command(command, APP_DIR)
                self.activity_logged.emit(f"[修复] {label}\n$ {command}\nexit={code}\n{output or '(no output)'}\n")
            self.diagnostics_ready.emit({item.key: item for item in collect_checks(APP_DIR)}, datetime.now().strftime("%H:%M:%S"))

        threading.Thread(target=worker, daemon=True).start()

    def _render_quickstart(self, snapshot) -> None:
        stage_lines = [
            self._step_line("1. 桌面依赖", not self._is_missing("pyside6") and not self._is_missing("psutil")),
            self._step_line("2. Node / Codex", not any(self._is_missing(key) for key in ["node", "npm", "codex"])),
            self._step_line("3. 微信账号文件", not self._is_missing("weixin_account")),
            self._step_line("4. 后台启动", snapshot.hub_running and snapshot.bridge_running),
        ]
        self.quickstart_steps.setPlainText(
            "\n".join(
                stage_lines
                + [
                    "",
                    "微信命令:",
                    "/help",
                    "/status",
                    "/new <name>",
                    "/list",
                    "/use <name>",
                    "/close",
                    "/reset",
                    "",
                    "微信账号目录:",
                    str((APP_DIR / "accounts").resolve()),
                ]
            )
        )

        if self.primary_action == "repair":
            self.quickstart_status.setText("先不用研究配置。点主按钮，桌面会按顺序自动补齐可安装的依赖。")
        elif self.primary_action == "login":
            self.quickstart_status.setText("依赖已经就绪，现在只需要把微信账号 json/sync 文件放进项目目录。")
        elif self.primary_action == "start":
            self.quickstart_status.setText("环境已准备好，现在只差启动后台服务。")
        elif self.primary_action == "stop":
            self.quickstart_status.setText("后台已经在运行。关闭桌面前，优先通过主按钮正常停止。")
        else:
            self.quickstart_status.setText("先看自动诊断区域，桌面会告诉你当前缺哪一步。")

    def _render_issues(self, snapshot, bridge_state: dict) -> None:
        issues: list[dict[str, str]] = []

        if any(self._is_missing(key) for key in ["pyside6", "psutil", "nvm", "node", "npm", "codex"]):
            issues.append(
                {
                    "kind": "dependencies",
                    "title": "依赖未就绪",
                    "detail": "先处理缺失依赖，再继续导入微信账号文件或启动后台。",
                }
            )

        if self._is_missing("weixin_account"):
            issues.append(
                {
                    "kind": "login",
                    "title": "微信账号文件缺失",
                    "detail": "当前没有可用的微信账号 json/sync 文件，需要先放入项目目录。",
                }
            )

        if snapshot.hub_running != snapshot.bridge_running:
            issues.append(
                {
                    "kind": "processes",
                    "title": "后台状态不一致",
                    "detail": "Hub 和 Bridge 没有同时处于运行状态，建议先停止，再重新启动。",
                }
            )

        if bridge_state.get("last_error"):
            issues.append(
                {
                    "kind": "logs",
                    "title": "微信桥最近报错",
                    "detail": str(bridge_state.get("last_error") or "").strip(),
                }
            )

        if snapshot.codex_processes and not (snapshot.hub_running or snapshot.bridge_running):
            issues.append(
                {
                    "kind": "processes",
                    "title": "检测到残留进程",
                    "detail": "后台已经停止，但仍检测到 Codex 相关进程，建议清理残留进程。",
                }
            )

        if not issues:
            self.issue_summary_label.setText("当前没有需要主人手动处理的异常。")
            self.issue_text.setPlainText("服务状态和基础依赖看起来正常。出现新问题时，这里会列出具体异常和对应操作。")
        else:
            self.issue_summary_label.setText(f"当前检测到 {len(issues)} 个需要处理的问题。")
            self.issue_text.setPlainText("\n\n".join(f"[{issue['title']}]\n{issue['detail']}" for issue in issues))

        issue_kinds = {issue["kind"] for issue in issues}
        self.issue_repair_button.setVisible("dependencies" in issue_kinds)
        self.issue_login_button.setVisible("login" in issue_kinds)
        self.issue_cleanup_button.setVisible("processes" in issue_kinds)
        self.issue_open_dir_button.setVisible(True)

    def _on_agent_selection_changed(self) -> None:
        hub_state = read_json(HUB_STATE_PATH)
        self._render_agent_detail(hub_state)

    def _on_agent_session_changed(self) -> None:
        hub_state = read_json(HUB_STATE_PATH)
        self._render_agent_detail(hub_state)

    def _render_agent_detail(self, hub_state: dict) -> None:
        session_name = self._selected_agent_id()
        if not session_name:
            self._reset_agent_session_list()
            self.agent_detail_text.setPlainText("先在上方选中一个会话。")
            self.agent_conversation_text.setPlainText("这里会显示该会话最近几轮对话。")
            return

        self.agent_session_list.blockSignals(True)
        self.agent_session_list.clear()
        self.agent_session_list.blockSignals(False)

        all_tasks = hub_state.get("tasks", [])
        tasks = [task for task in all_tasks if self._normalize_task_session_name(task) == session_name][:8]
        tasks = sorted(tasks, key=lambda item: str(item.get("created_at") or ""), reverse=True)

        session_dir = APP_DIR / ".runtime" / "sessions"
        selected_session_file = self._session_file_for_name(session_dir, session_name)
        selected_session_id = ""
        if selected_session_file.exists():
            selected_session_id = selected_session_file.read_text(encoding="utf-8").strip()

        queue_size = sum(1 for task in tasks if task.get("status") in {"queued", "running"})
        success_count = sum(1 for task in all_tasks if self._normalize_task_session_name(task) == session_name and task.get("status") == "succeeded")
        failure_count = sum(1 for task in all_tasks if self._normalize_task_session_name(task) == session_name and task.get("status") == "failed")
        if queue_size:
            status = "running" if any(task.get("status") == "running" for task in tasks) else "queued"
        elif tasks:
            status = str(tasks[0].get("status") or "idle")
        else:
            status = "idle"

        detail_lines = [
            f"会话名: {session_name}",
            f"状态: {status}",
            f"队列: {queue_size}",
            f"成功/失败: {success_count}/{failure_count}",
            f"会话文件: {selected_session_file}",
            f"当前会话 ID: {selected_session_id or '(empty)'}",
        ]

        if tasks:
            detail_lines.extend(["", "最近任务:"])
            for task in tasks:
                detail_lines.append(
                    f"[{task.get('status')}] {task.get('created_at')}  session={self._normalize_task_session_name(task)}  source={task.get('source') or '-'}"
                )
                detail_lines.append(str(task.get("prompt") or ""))
                if task.get("output"):
                    detail_lines.append(f"output: {str(task.get('output'))[:240]}")
                if task.get("error"):
                    detail_lines.append(f"error: {str(task.get('error'))[:240]}")
                detail_lines.append("")

            conversation_lines = ["会话预览:"]
            for index, task in enumerate(reversed(tasks[-6:]), start=1):
                conversation_lines.append(f"第 {index} 轮 | {task.get('created_at')}")
                conversation_lines.append(f"用户: {str(task.get('prompt') or '(empty)')[:320]}")
                if task.get("output"):
                    conversation_lines.append(f"Codex: {str(task.get('output') or '')[:320]}")
                elif task.get("error"):
                    conversation_lines.append(f"错误: {str(task.get('error') or '')[:320]}")
                else:
                    conversation_lines.append("Codex: (no output)")
                conversation_lines.append("")
        else:
            conversation_lines = ["会话预览:", "当前选择下还没有任务记录。"]

        self.agent_detail_text.setPlainText("\n".join(detail_lines).strip())
        self.agent_conversation_text.setPlainText("\n".join(conversation_lines).strip())

    def _selected_agent_id(self) -> str:
        row = self.agent_table.currentRow()
        if row < 0:
            return ""
        item = self.agent_table.item(row, 0)
        if item is None:
            return ""
        return str(item.data(Qt.UserRole) or "")

    def _selected_agent_session_name(self) -> str:
        item = self.agent_session_list.currentItem()
        if item is None:
            return "__all__"
        return str(item.data(Qt.UserRole) or "__all__")

    def toggle_auto_refresh(self) -> None:
        self._auto_refresh_enabled = self.auto_refresh_button.isChecked()
        self.auto_refresh_button.setText(f"自动刷新：{'开' if self._auto_refresh_enabled else '关'}")
        if self._auto_refresh_enabled:
            self.refresh_runtime()
            self.refresh_diagnostics()

    def _reset_agent_session_list(self) -> None:
        self.agent_session_list.blockSignals(True)
        self.agent_session_list.clear()
        default_item = QListWidgetItem("全部会话")
        default_item.setData(Qt.UserRole, "__all__")
        self.agent_session_list.addItem(default_item)
        self.agent_session_list.setCurrentRow(0)
        self.agent_session_list.blockSignals(False)

    @staticmethod
    def _session_name_from_file(agent_id: str, session_file: Path) -> str:
        stem = session_file.stem
        if "__" in stem:
            return stem.split("__", 1)[1] or "default"
        if not agent_id or stem == agent_id:
            return "default"
        prefix = f"{agent_id}__"
        if stem.startswith(prefix):
            return stem[len(prefix) :] or "default"
        return stem

    @staticmethod
    def _session_file_for_name(session_dir: Path, session_name: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in session_name).strip("-_") or "default"
        if safe == "default":
            direct = session_dir / "main.txt"
            if direct.exists():
                return direct
            fallback = sorted(session_dir.glob("*.txt"))
            return fallback[0] if fallback else session_dir / "main.txt"
        exact_matches = sorted(session_dir.glob(f"*__{safe}.txt"))
        if exact_matches:
            return exact_matches[0]
        return session_dir / f"main__{safe}.txt"

    @staticmethod
    def _normalize_task_session_name(task: dict) -> str:
        return str(task.get("session_name") or "default")

    @staticmethod
    def _build_hub_signature(hub_state: dict) -> tuple:
        agents = tuple(
            (
                agent.get("id"),
                (agent.get("runtime") or {}).get("status"),
                (agent.get("runtime") or {}).get("queue_size"),
                (agent.get("runtime") or {}).get("success_count"),
                (agent.get("runtime") or {}).get("failure_count"),
                (agent.get("runtime") or {}).get("updated_at"),
            )
            for agent in hub_state.get("agents", [])
        )
        tasks = tuple(
            (
                task.get("id"),
                task.get("status"),
                task.get("finished_at"),
                task.get("session_name"),
                task.get("agent_id"),
            )
            for task in hub_state.get("tasks", [])[:20]
        )
        return agents, tasks

    def _is_missing(self, key: str) -> bool:
        item = self.checks.get(key)
        return bool(item and not item.ok)

    def _maybe_prompt_auto_repair(self) -> None:
        if self._auto_repair_prompted:
            return
        if self.primary_action != "repair":
            return
        self._auto_repair_prompted = True
        should_run = QMessageBox.question(
            self,
            "检测到缺失依赖",
            "当前检测到可自动安装的依赖。现在直接执行一键补齐吗？",
        )
        if should_run == QMessageBox.Yes:
            self.repair_environment()

    def refresh_runtime(self) -> None:
        if not self._auto_refresh_enabled and self.sender() is self.runtime_timer:
            return
        snapshot = get_runtime_snapshot()
        hub_state = read_json(HUB_STATE_PATH)
        bridge_state = read_json(BRIDGE_STATE_PATH)

        if snapshot.hub_running and snapshot.bridge_running:
            self.stack_badge.setText(self._t("ui.status.running"))
            self.stack_badge.setStyleSheet("background:#d9f3e4;color:#12633b;border:1px solid #b9dfca;border-radius:16px;padding:10px 12px;font-weight:700;")
        elif snapshot.hub_running or snapshot.bridge_running:
            self.stack_badge.setText(self._t("ui.status.partial"))
            self.stack_badge.setStyleSheet("background:#fff2cc;color:#946200;border:1px solid #eadcb0;border-radius:16px;padding:10px 12px;font-weight:700;")
        else:
            self.stack_badge.setText(self._t("ui.status.stopped"))
            self.stack_badge.setStyleSheet("background:#f8d7da;color:#8a1c2b;border:1px solid #efbac2;border-radius:16px;padding:10px 12px;font-weight:700;")

        overview_lines = [
            self._t("ui.overview.hub", status=self._t("ui.status.running") if snapshot.hub_running else self._t("ui.status.stopped"), pid=self._pid_text(snapshot.hub_pid)),
            self._t("ui.overview.bridge", status=self._t("ui.status.running") if snapshot.bridge_running else self._t("ui.status.stopped"), pid=self._pid_text(snapshot.bridge_pid)),
            self._t("ui.overview.agent_processes", count=len(snapshot.codex_processes)),
            self._t("ui.overview.active_account", account=BridgeConfig.load().active_account_id),
            "",
        ]
        if snapshot.codex_processes:
            overview_lines.extend(snapshot.codex_processes[:8])
        else:
            overview_lines.append(self._t("ui.overview.none_agents"))

        if bridge_state:
            overview_lines.extend(["", self._t("ui.overview.bridge_state")])
            overview_lines.extend(f"{key}: {value}" for key, value in list(bridge_state.items())[:8])

        self.overview_text.setPlainText("\n".join(overview_lines))
        hub_signature = self._build_hub_signature(hub_state)
        if hub_signature != self._last_hub_signature:
            self._render_agents(hub_state)
            self._last_hub_signature = hub_signature
        self._render_issues(snapshot, bridge_state)
        self._refresh_summary(snapshot)

    def _refresh_summary(self, snapshot) -> None:
        missing = [item.label for item in self.checks.values() if not item.ok] if self.checks else []
        if missing:
            self.summary_label.setText(self._t("ui.summary.missing", count=len(missing), items="、".join(missing[:4])))
        elif snapshot.hub_running and snapshot.bridge_running:
            self.summary_label.setText(self._t("ui.summary.ready_running"))
        else:
            self.summary_label.setText(self._t("ui.summary.ready_waiting"))

        action_key, label, hint = self._decide_primary_action(snapshot)
        self.primary_action = action_key
        self.primary_button.setText(label)
        self.next_step_label.setText(hint)
        self._render_quickstart(snapshot)

    def run_command(self, label: str, command: str) -> None:
        should_run = QMessageBox.question(self, self._t("ui.confirm.run.title"), self._t("ui.confirm.run.body", label=label, command=command))
        if should_run != QMessageBox.Yes:
            return

        def worker() -> None:
            code, output = run_shell_command(command, APP_DIR)
            self.activity_logged.emit(self._t("ui.activity.command", label=label, command=command, code=code, output=output or "(no output)"))
            self.diagnostics_ready.emit({item.key: item for item in collect_checks(APP_DIR)}, datetime.now().strftime("%H:%M:%S"))

        threading.Thread(target=worker, daemon=True).start()

    def run_async_action(self, label: str, action) -> None:
        def worker() -> None:
            for line in action():
                self.activity_logged.emit(self._t("ui.activity.action", label=label, line=line))
            self.runtime_refresh_requested.emit()

        threading.Thread(target=worker, daemon=True).start()

    def confirm_emergency_stop(self) -> None:
        should_run = QMessageBox.question(self, self._t("ui.confirm.cleanup.title"), self._t("ui.confirm.cleanup.body"))
        if should_run == QMessageBox.Yes:
            self.run_async_action(self._t("ui.button.cleanup"), emergency_stop)

    def open_path(self, path: Path) -> None:
        if not path.exists():
            QMessageBox.critical(self, self._t("ui.error.path_missing.title"), str(path))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _show_qr_login_dialog(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(self._t("ui.account.qr_login.title"))
        dialog.setMinimumSize(400, 520)
        layout = QVBoxLayout(dialog)

        status_label = QLabel(self._t("ui.account.qr_login.getting"))
        status_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(status_label)

        qr_label = QLabel()
        qr_label.setAlignment(Qt.AlignCenter)
        qr_label.setMinimumSize(300, 300)
        layout.addWidget(qr_label)

        hint_label = QLabel(self._t("ui.account.qr_login.hint"))
        hint_label.setAlignment(Qt.AlignCenter)
        hint_label.setWordWrap(True)
        layout.addWidget(hint_label)

        btn_layout = QHBoxLayout()
        cancel_btn = QPushButton(self._t("ui.button.cancel"))
        cancel_btn.clicked.connect(dialog.reject)
        btn_layout.addWidget(cancel_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        qr_code: str = ""
        login_finished = False

        def fetch_qrcode() -> None:
            nonlocal qr_code
            try:
                url = f"{self._ilink_base_url()}/ilink/bot/get_bot_qrcode?bot_type=3"
                headers = {
                    "AuthorizationType": "ilink_bot_token",
                    "iLink-App-Id": "bot",
                    "iLink-App-ClientVersion": "131073",
                }
                req = urllib.request.Request(url=url, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    qr_code = data.get("qrcode", "")
                    if qr_code:
                        pixmap = self._generate_qr_pixmap(qr_code)
                        qr_label.setPixmap(pixmap)
                        status_label.setText(self._t("ui.account.qr_login.scan"))
                        QTimer.singleShot(100, lambda: poll_status())
                    else:
                        status_label.setText(self._t("ui.account.qr_login.error"))
            except Exception as exc:
                status_label.setText(self._t("ui.account.qr_login.error_detail", error=str(exc)))

        def poll_status() -> None:
            nonlocal login_finished
            if login_finished or not qr_code:
                return
            try:
                url = f"{self._ilink_base_url()}/ilink/bot/get_qrcode_status?qrcode={urllib.parse.quote(qr_code)}"
                headers = {
                    "AuthorizationType": "ilink_bot_token",
                    "iLink-App-Id": "bot",
                    "iLink-App-ClientVersion": "131073",
                }
                req = urllib.request.Request(url=url, headers=headers)
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    status = data.get("status", "")
                    if status == "confirmed":
                        login_finished = True
                        status_label.setText(self._t("ui.account.qr_login.success"))
                        hint_label.setText(self._t("ui.account.qr_login.saving"))
                        save_account(data)
                    elif status == "expired":
                        status_label.setText(self._t("ui.account.qr_login.expired"))
                        hint_label.setText(self._t("ui.account.qr_login.retry"))
                    else:
                        QTimer.singleShot(1000, poll_status)
            except urllib.error.HTTPError:
                QTimer.singleShot(1000, poll_status)
            except Exception:
                QTimer.singleShot(3000, poll_status)

        def save_account(data: dict) -> None:
            try:
                account_id = data.get("bot_info", {}).get("name", f"wechat-{datetime.now().strftime('%Y%m%d%H%M%S')}")
                account_file = APP_DIR / "accounts" / f"{account_id}.json"
                sync_file = APP_DIR / "accounts" / f"{account_id}.sync.json"

                account_file.parent.mkdir(parents=True, exist_ok=True)
                account_info = {
                    "token": data.get("bot_token", ""),
                    "baseUrl": data.get("base_url", self._ilink_base_url()),
                    "name": account_id,
                }
                account_file.write_text(json.dumps(account_info, ensure_ascii=False, indent=2), encoding="utf-8")
                sync_file.write_text(json.dumps({"get_updates_buf": ""}, ensure_ascii=False), encoding="utf-8")

                config = BridgeConfig.load()
                new_profile = config.add_account(account_id, str(account_file), str(sync_file))
                if new_profile:
                    config.set_active_account(new_profile.account_id)
                    config.save()
                    self.activity_logged.emit(self._t("ui.account.qr_login.saved", account=account_id))
                    self.refresh_runtime()
                    QTimer.singleShot(500, dialog.accept)
                else:
                    hint_label.setText(self._t("ui.account.qr_login.save_failed"))
            except Exception as exc:
                hint_label.setText(self._t("ui.account.qr_login.save_failed_detail", error=str(exc)))

        threading.Thread(target=fetch_qrcode, daemon=True).start()
        dialog.exec()

    def _ilink_base_url(self) -> str:
        try:
            config = BridgeConfig.load()
            if config.accounts:
                first_account = config.accounts[0]
                if first_account.account_path.exists():
                    data = json.loads(first_account.account_path.read_text(encoding="utf-8"))
                    base_url = data.get("baseUrl", "").strip()
                    if base_url:
                        return base_url
        except Exception:
            pass
        return "https://ilinkai.weixin.qq.com"

    def _generate_qr_pixmap(self, data: str) -> QPixmap:
        try:
            import qrcode
            qr = qrcode.QRCode(version=3, box_size=8, border=2)
            qr.add_data(data)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            pixmap = QPixmap()
            pixmap.loadFromData(buf.getvalue())
            return pixmap
        except Exception:
            return QPixmap()

    def _account_option_text(self, config: BridgeConfig, account) -> str:
        status = self._t("ui.account.status.ready") if account.is_usable else self._t("ui.account.status.missing")
        marker = self._t("ui.account.option.active") if account.account_id == config.active_account_id else self._t("ui.account.option.inactive")
        return self._t(
            "ui.account.option.label",
            marker=marker,
            account=account.account_id,
            file=Path(account.account_file).name,
            status=status,
        )

    def _restart_services_after_account_switch(self, account_id: str) -> None:
        def worker() -> None:
            for line in stop_all():
                self.activity_logged.emit(self._t("ui.activity.action", label=self._t("ui.account.restart.label", account=account_id), line=line))
            for line in start_all():
                self.activity_logged.emit(self._t("ui.activity.action", label=self._t("ui.account.restart.label", account=account_id), line=line))
            self.diagnostics_ready.emit({item.key: item for item in collect_checks(APP_DIR)}, datetime.now().strftime("%H:%M:%S"))
            self.runtime_refresh_requested.emit()

        threading.Thread(target=worker, daemon=True).start()

    def configure_accounts(self) -> None:
        config = BridgeConfig.load()
        
        options = []
        option_map = {}
        
        if config.accounts:
            for account in config.accounts:
                text = self._account_option_text(config, account)
                option_map[text] = ("existing", account)
                options.append(text)
        
        qr_login_text = self._t("ui.account.qr_login.option")
        option_map[qr_login_text] = ("qr_login", None)
        options.append(qr_login_text)
        
        current_index = 0
        if config.accounts:
            for index, account in enumerate(config.accounts):
                if account.account_id == config.active_account_id:
                    current_index = index
                    break
        
        selected_text, accepted = QInputDialog.getItem(
            self,
            self._t("ui.account.dialog.title"),
            self._t("ui.account.dialog.label"),
            options,
            current_index,
            False,
        )
        if not accepted or not selected_text:
            return

        choice = option_map[selected_text]
        if choice[0] == "qr_login":
            self._show_qr_login_dialog()
            return

        selected = choice[1]
        if selected.account_id == config.active_account_id:
            self.activity_logged.emit(self._t("ui.account.already_active", account=selected.account_id))
            self.refresh_diagnostics()
            return

        config.set_active_account(selected.account_id)
        config.save()
        self.activity_logged.emit(self._t("ui.account.activated", account=selected.account_id))
        snapshot = get_runtime_snapshot()
        if snapshot.hub_running or snapshot.bridge_running:
            self._restart_services_after_account_switch(selected.account_id)
            return
        self.refresh_runtime()
        self.refresh_diagnostics()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        snapshot = get_runtime_snapshot()
        if snapshot.hub_running or snapshot.bridge_running:
            should_stop = QMessageBox.question(self, self._t("ui.confirm.close.title"), self._t("ui.confirm.close.body"))
            if should_stop == QMessageBox.Yes:
                for line in stop_all():
                    self.activity_logged.emit(self._t("ui.activity.action", label=self._t("ui.primary.stop.label"), line=line))
        event.accept()

    def _apply_diagnostics(self, checks: dict, diag_at: str) -> None:
        self._diagnostics_running = False
        self.checks = checks
        self.last_diag_at = diag_at
        self.diag_time_label.setText(self._t("ui.diagnostics.label", time=diag_at))

        ordered_keys = ["python", "winget", "nvm", "pyside6", "psutil", "node", "npm", "codex", "opencode", "weixin_account", "project_files"]
        lines: list[str] = []
        for key in ordered_keys:
            item = checks.get(key)
            if item is None:
                continue
            status = self._t("ui.diagnostics.ok") if item.ok else self._t("ui.diagnostics.missing")
            lines.append(f"[{status}] {item.label}: {item.detail}")
        self.diag_text.setPlainText("\n".join(lines))
        self.refresh_runtime()
        self._maybe_prompt_auto_repair()

    def _decide_primary_action(self, snapshot) -> tuple[str, str, str]:
        checks = self.checks
        if snapshot.hub_running or snapshot.bridge_running:
            return "stop", self._t("ui.primary.stop.label"), self._t("ui.primary.stop.hint")

        blocking = [key for key in ["python", "project_files"] if checks.get(key) and not checks[key].ok]
        if self._is_missing("nvm") and self._is_missing("winget"):
            blocking.append("nvm")
        if blocking:
            return "manual", self._t("ui.primary.manual.label"), self._t("ui.primary.manual.hint")

        auto_fixable = [key for key in ["pyside6", "psutil", "nvm", "node", "npm", "codex", "opencode"] if checks.get(key) and not checks[key].ok]
        if auto_fixable:
            return "repair", self._t("ui.primary.repair.label"), self._t("ui.primary.repair.hint")

        if checks.get("weixin_account") and not checks["weixin_account"].ok:
            return "login", self._t("ui.primary.login.label"), self._t("ui.primary.login.hint")

        return "start", self._t("ui.primary.start.label"), self._t("ui.primary.start.hint")

    def repair_environment(self) -> None:
        self._auto_repair_prompted = True
        commands: list[tuple[str, str]] = []
        will_have_node = not (self._is_missing("node") or self._is_missing("npm"))
        if self._is_missing("pyside6") or self._is_missing("psutil"):
            commands.append((self._t("ui.quickstart.step.desktop"), "python -m pip install PySide6 psutil"))
        if self._is_missing("nvm") and not self._is_missing("winget"):
            commands.append(("NVM for Windows", "winget install CoreyButler.NVMforWindows --accept-package-agreements --accept-source-agreements"))
        if self._is_missing("node") or self._is_missing("npm"):
            commands.append(("Node 24.14.1", build_nvm_node_command()))
            will_have_node = True
        if self._is_missing("codex") and will_have_node:
            commands.append(("Codex CLI", "npm.cmd install -g codex"))
        if self._is_missing("opencode") and will_have_node:
            commands.append(("OpenCode CLI", "npm.cmd install -g opencode-ai"))

        if not commands:
            QMessageBox.information(self, self._t("ui.info.no_repair.title"), self._t("ui.info.no_repair.body"))
            return

        summary = "\n".join(f"- {label}: {command}" for label, command in commands)
        should_run = QMessageBox.question(self, self._t("ui.confirm.repair.title"), self._t("ui.confirm.repair.body", summary=summary))
        if should_run != QMessageBox.Yes:
            return

        def worker() -> None:
            for label, command in commands:
                code, output = run_shell_command(command, APP_DIR)
                self.activity_logged.emit(self._t("ui.activity.command", label=label, command=command, code=code, output=output or "(no output)"))
            self.diagnostics_ready.emit({item.key: item for item in collect_checks(APP_DIR)}, datetime.now().strftime("%H:%M:%S"))

        threading.Thread(target=worker, daemon=True).start()

    def _render_quickstart(self, snapshot) -> None:
        stage_lines = [
            self._step_line(self._t("ui.quickstart.step.desktop"), not self._is_missing("pyside6") and not self._is_missing("psutil")),
            self._step_line(self._t("ui.quickstart.step.node"), not any(self._is_missing(key) for key in ["node", "npm", "codex", "opencode"])),
            self._step_line(self._t("ui.quickstart.step.accounts"), not self._is_missing("weixin_account")),
            self._step_line(self._t("ui.quickstart.step.start"), snapshot.hub_running and snapshot.bridge_running),
        ]
        self.quickstart_steps.setPlainText(
            "\n".join(
                stage_lines
                + [
                    "",
                    self._t("ui.quickstart.commands"),
                    "/help",
                    "/status",
                    "/new <name>",
                    "/list",
                    "/use <name>",
                    "/backend",
                    "/backend <codex|opencode>",
                    "/close",
                    "/reset",
                    "",
                    self._t("ui.quickstart.accounts_dir"),
                    str((APP_DIR / "accounts").resolve()),
                ]
            )
        )

        status_map = {
            "repair": self._t("ui.quickstart.repair"),
            "login": self._t("ui.quickstart.login"),
            "start": self._t("ui.quickstart.start"),
            "stop": self._t("ui.quickstart.stop"),
        }
        self.quickstart_status.setText(status_map.get(self.primary_action, self._t("ui.quickstart.manual")))

    def _render_issues(self, snapshot, bridge_state: dict) -> None:
        issues: list[dict[str, str]] = []
        if any(self._is_missing(key) for key in ["pyside6", "psutil", "nvm", "node", "npm", "codex", "opencode"]):
            issues.append({"kind": "dependencies", "title": self._t("ui.issue.dependencies.title"), "detail": self._t("ui.issue.dependencies.detail")})
        if self._is_missing("weixin_account"):
            issues.append({"kind": "login", "title": self._t("ui.issue.login.title"), "detail": self._t("ui.issue.login.detail")})
        if snapshot.hub_running != snapshot.bridge_running:
            issues.append({"kind": "processes", "title": self._t("ui.issue.process_mismatch.title"), "detail": self._t("ui.issue.process_mismatch.detail")})
        if bridge_state.get("last_error"):
            issues.append({"kind": "logs", "title": self._t("ui.issue.logs.title"), "detail": str(bridge_state.get("last_error") or "").strip()})
        if snapshot.codex_processes and not (snapshot.hub_running or snapshot.bridge_running):
            issues.append({"kind": "processes", "title": self._t("ui.issue.residual.title"), "detail": self._t("ui.issue.residual.detail")})

        if not issues:
            self.issue_summary_label.setText(self._t("ui.issue.none.summary"))
            self.issue_text.setPlainText(self._t("ui.issue.none.detail"))
        else:
            self.issue_summary_label.setText(self._t("ui.issue.summary.count", count=len(issues)))
            self.issue_text.setPlainText("\n\n".join(f"[{issue['title']}]\n{issue['detail']}" for issue in issues))

        issue_kinds = {issue["kind"] for issue in issues}
        self.issue_repair_button.setVisible("dependencies" in issue_kinds)
        self.issue_manage_accounts_button.setVisible(True)
        self.issue_login_button.setVisible("login" in issue_kinds)
        self.issue_cleanup_button.setVisible("processes" in issue_kinds)
        self.issue_open_dir_button.setVisible(True)

    def _render_agent_detail(self, hub_state: dict) -> None:
        session_name = self._selected_agent_id()
        if not session_name:
            self._reset_agent_session_list()
            self.agent_detail_text.setPlainText(self._t("ui.agent.select_session"))
            self.agent_conversation_text.setPlainText(self._t("ui.agent.select_preview"))
            return

        self.agent_session_list.blockSignals(True)
        self.agent_session_list.clear()
        self.agent_session_list.blockSignals(False)

        all_tasks = hub_state.get("tasks", [])
        tasks = [task for task in all_tasks if self._normalize_task_session_name(task) == session_name][:8]
        tasks = sorted(tasks, key=lambda item: str(item.get("created_at") or ""), reverse=True)

        session_dir = APP_DIR / ".runtime" / "sessions"
        selected_session_file = self._session_file_for_name(session_dir, session_name)
        selected_session_id = selected_session_file.read_text(encoding="utf-8").strip() if selected_session_file.exists() else ""

        queue_size = sum(1 for task in tasks if task.get("status") in {"queued", "running"})
        success_count = sum(1 for task in all_tasks if self._normalize_task_session_name(task) == session_name and task.get("status") == "succeeded")
        failure_count = sum(1 for task in all_tasks if self._normalize_task_session_name(task) == session_name and task.get("status") == "failed")
        if queue_size:
            status = "running" if any(task.get("status") == "running" for task in tasks) else "queued"
        elif tasks:
            status = str(tasks[0].get("status") or "idle")
        else:
            status = "idle"

        detail_lines = [
            self._t("ui.agent.detail.session", value=session_name),
            self._t("ui.agent.detail.status", value=self._task_status_text(status)),
            self._t("ui.agent.detail.queue", value=queue_size),
            self._t("ui.agent.detail.result", success=success_count, failure=failure_count),
            self._t("ui.agent.detail.file", value=selected_session_file),
            self._t("ui.agent.detail.id", value=selected_session_id or "(empty)"),
        ]

        if tasks:
            detail_lines.extend(["", self._t("ui.agent.detail.recent")])
            for task in tasks:
                detail_lines.append(f"[{self._task_status_text(str(task.get('status') or 'idle'))}] {task.get('created_at')}  session={self._normalize_task_session_name(task)}  source={task.get('source') or '-'}")
                detail_lines.append(str(task.get("prompt") or ""))
                if task.get("output"):
                    detail_lines.append(f"output: {str(task.get('output'))[:240]}")
                if task.get("error"):
                    detail_lines.append(f"error: {str(task.get('error'))[:240]}")
                detail_lines.append("")

            conversation_lines = [self._t("ui.agent.preview.title")]
            for index, task in enumerate(reversed(tasks[-6:]), start=1):
                conversation_lines.append(self._t("ui.agent.preview.round", index=index, time=task.get("created_at")))
                conversation_lines.append(self._t("ui.agent.preview.user", text=str(task.get("prompt") or "(empty)")[:320]))
                if task.get("output"):
                    conversation_lines.append(self._t("ui.agent.preview.assistant", text=str(task.get("output") or "")[:320]))
                elif task.get("error"):
                    conversation_lines.append(self._t("ui.agent.preview.error", text=str(task.get("error") or "")[:320]))
                else:
                    conversation_lines.append(self._t("ui.agent.preview.no_output"))
                conversation_lines.append("")
        else:
            conversation_lines = [self._t("ui.agent.preview.title"), self._t("ui.agent.preview.none")]

        self.agent_detail_text.setPlainText("\n".join(detail_lines).strip())
        self.agent_conversation_text.setPlainText("\n".join(conversation_lines).strip())

    def toggle_auto_refresh(self) -> None:
        self._auto_refresh_enabled = self.auto_refresh_button.isChecked()
        self.auto_refresh_button.setText(self._t("ui.auto_refresh.on") if self._auto_refresh_enabled else self._t("ui.auto_refresh.off"))
        if self._auto_refresh_enabled:
            self.refresh_runtime()
            self.refresh_diagnostics()

    def _reset_agent_session_list(self) -> None:
        self.agent_session_list.blockSignals(True)
        self.agent_session_list.clear()
        default_item = QListWidgetItem(self._t("ui.agent.all_sessions"))
        default_item.setData(Qt.UserRole, "__all__")
        self.agent_session_list.addItem(default_item)
        self.agent_session_list.setCurrentRow(0)
        self.agent_session_list.blockSignals(False)

    def _maybe_prompt_auto_repair(self) -> None:
        if self._auto_repair_prompted:
            return
        if self.primary_action != "repair":
            return
        self._auto_repair_prompted = True
        should_run = QMessageBox.question(self, self._t("ui.confirm.auto_repair.title"), self._t("ui.confirm.auto_repair.body"))
        if should_run == QMessageBox.Yes:
            self.repair_environment()

    def _step_line(self, title: str, done: bool) -> str:
        return self._t("ui.step.done", title=title) if done else self._t("ui.step.pending", title=title)

    @staticmethod
    def _pid_text(pid: int | None) -> str:
        return f"(PID {pid})" if pid else ""


def main() -> int:
    app = QApplication(sys.argv)
    icon_path = APP_DIR / "codex_wechat_desktop.ico"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    window = MainWindow()
    window.show()
    window.raise_()
    window.activateWindow()
    QTimer.singleShot(0, window.raise_)
    QTimer.singleShot(0, window.activateWindow)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
