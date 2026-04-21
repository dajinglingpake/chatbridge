from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.app_service import ServiceResult
from core.mcp_service import (
    ManagerControlState,
    delegate_task,
    enter_control_mode,
    exit_control_mode,
    get_control_mode_state,
    get_management_snapshot,
    run_sender_command,
    start_agent_session,
    list_sender_conversations,
)


class McpServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.app_dir = Path(self._tempdir.name)
        self.manager_state_path = self.app_dir / ".runtime" / "state" / "chatbridge_manager_state.json"

        self.app_dir_patch = patch("core.mcp_service.APP_DIR", self.app_dir)
        self.state_path_patch = patch("core.mcp_service.MANAGER_STATE_PATH", self.manager_state_path)
        self.app_dir_patch.start()
        self.state_path_patch.start()
        self.addCleanup(self.app_dir_patch.stop)
        self.addCleanup(self.state_path_patch.stop)

    def test_enter_and_exit_control_mode_updates_state(self) -> None:
        entered = enter_control_mode("test-note")
        self.assertTrue(entered.ok)
        self.assertTrue(ManagerControlState.from_dict(get_control_mode_state().data).active)

        exited = exit_control_mode()
        self.assertTrue(exited.ok)
        self.assertFalse(ManagerControlState.from_dict(get_control_mode_state().data).active)

    def test_delegate_task_requires_control_mode(self) -> None:
        result = delegate_task("main", "ship it")
        self.assertFalse(result.ok)
        self.assertIn("未进入管理模式", result.summary)

    def test_run_sender_command_requires_control_mode(self) -> None:
        result = run_sender_command("sender-a", "/status")
        self.assertFalse(result.ok)
        self.assertIn("未进入管理模式", result.summary)

    def test_run_sender_command_can_skip_control_mode_for_trusted_manager(self) -> None:
        fake_bridge = SimpleNamespace(_handle_control_command=lambda sender_id, command: ("ok", True))
        with patch("core.mcp_service.WeixinBridge", return_value=fake_bridge):
            with patch("core.mcp_service.BridgeConfig.load", return_value=SimpleNamespace()):
                result = run_sender_command("sender-a", "/status", require_control_mode=False)
        self.assertTrue(result.ok)
        self.assertIn("已对发送方 sender-a 执行桥命令 /status", result.summary)

    def test_start_agent_session_submits_first_prompt(self) -> None:
        enter_control_mode()
        fake_agent = SimpleNamespace(
            id="reviewer",
            name="Reviewer",
            session_file=str(self.app_dir / "sessions" / "reviewer.txt"),
            workdir=str(self.app_dir / "workspace" / "reviewer"),
            backend="codex",
            model="",
            enabled=True,
        )
        with patch("core.mcp_service.HubConfig.load", return_value=SimpleNamespace(agents=[fake_agent])):
            with patch("core.mcp_service.submit_hub_task", return_value=ServiceResult(ok=True, message="任务已入队")) as mocked_submit:
                result = start_agent_session("reviewer", "deep-dive", "analyze the codebase")
        self.assertTrue(result.ok)
        mocked_submit.assert_called_once()
        self.assertEqual("deep-dive", result.data["session_name"])

    def test_start_agent_session_rejects_existing_session(self) -> None:
        enter_control_mode()
        sessions_dir = self.app_dir / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        existing_session_file = sessions_dir / "reviewer__deep-dive.txt"
        existing_session_file.write_text("thread-123", encoding="utf-8")
        fake_agent = SimpleNamespace(
            id="reviewer",
            name="Reviewer",
            session_file=str(self.app_dir / "sessions" / "reviewer.txt"),
            workdir=str(self.app_dir / "workspace" / "reviewer"),
            backend="codex",
            model="",
            enabled=True,
        )
        with patch("core.mcp_service.HubConfig.load", return_value=SimpleNamespace(agents=[fake_agent])):
            result = start_agent_session("reviewer", "deep-dive", "analyze the codebase")
        self.assertFalse(result.ok)
        self.assertIn("Agent 会话已存在", result.summary)

    def test_management_snapshot_includes_relation_lines(self) -> None:
        fake_agent = SimpleNamespace(
            id="main",
            name="Main",
            session_file=str(self.app_dir / "sessions" / "main.txt"),
            workdir=str(self.app_dir / "workspace" / "main"),
            backend="codex",
            model="gpt-5.4",
            enabled=True,
        )
        fake_binding = SimpleNamespace(
            manager_mode=True,
            get_current_session=lambda **kwargs: ("default", SimpleNamespace(backend="codex", model="", workdir="")),
            sessions={"default": SimpleNamespace(backend="codex", model="", workdir="")},
        )
        fake_dashboard = SimpleNamespace(
            snapshot=SimpleNamespace(bridge_running=True, hub_running=True),
            bridge_conversations={"sender-a": fake_binding},
            hub_state=SimpleNamespace(agents=[], tasks=[]),
        )
        fake_config = SimpleNamespace(backend_id="main", default_backend="codex", active_account_id="wechat-bot")
        with patch("core.mcp_service.BridgeConfig.load", return_value=fake_config):
            with patch("core.mcp_service.HubConfig.load", return_value=SimpleNamespace(agents=[fake_agent])):
                with patch("core.mcp_service.load_dashboard_state", return_value=fake_dashboard):
                    result = get_management_snapshot("sender-a")
        self.assertTrue(result.ok)
        self.assertIn("control_mode", result.data)
        self.assertTrue(result.data["target_sender"]["wechat_manager_mode"])
        self.assertTrue(any("你的业务会话（不含管理助手）：" in line for line in result.data["target_sender"]["summary_lines"]))
        self.assertIn("你的业务会话（不含管理助手）：当前会话 default，共 1 个会话", result.summary)
        self.assertEqual("暂无历史", result.data["target_sender"]["latest_manager_reply_summary"])
        self.assertNotIn("管理助手最后回复：", result.summary)
        self.assertNotIn("微信管理 Agent", result.summary)
        relation_lines = result.data["target_sender"]["relation_lines"]
        self.assertTrue(any("Agent main" in line for line in relation_lines))
        self.assertTrue(any("Session default" in line for line in relation_lines))

    def test_management_snapshot_includes_recent_events(self) -> None:
        fake_agent = SimpleNamespace(
            id="main",
            name="Main",
            session_file=str(self.app_dir / "sessions" / "main.txt"),
            workdir=str(self.app_dir / "workspace" / "main"),
            backend="codex",
            model="gpt-5.4",
            enabled=True,
        )
        fake_dashboard = SimpleNamespace(
            snapshot=SimpleNamespace(bridge_running=True, hub_running=True),
            bridge_conversations={},
            hub_state=SimpleNamespace(agents=[], tasks=[]),
        )
        fake_config = SimpleNamespace(backend_id="main", default_backend="codex", active_account_id="wechat-bot")
        event_log_path = self.app_dir / ".runtime" / "logs" / "weixin_bridge_events.jsonl"
        event_log_path.parent.mkdir(parents=True, exist_ok=True)
        event_log_path.write_text(
            '{"at":"2026-04-20T12:00:00","event":"accepted","task_id":"task-a","sender_id":"sender-a","session_name":"default"}\n',
            encoding="utf-8",
        )
        with patch("core.mcp_service.EVENT_LOG_PATH", event_log_path):
            with patch("core.mcp_service.BridgeConfig.load", return_value=fake_config):
                with patch("core.mcp_service.HubConfig.load", return_value=SimpleNamespace(agents=[fake_agent])):
                    with patch("core.mcp_service.load_dashboard_state", return_value=fake_dashboard):
                        result = get_management_snapshot("sender-a")
        self.assertTrue(result.ok)
        self.assertEqual("accepted", result.data["recent_events"][0]["event"])
        self.assertEqual("accepted", result.data["target_sender"]["recent_events"][0]["event"])

    def test_management_snapshot_shows_session_last_reply_even_for_short_greeting(self) -> None:
        binding_a = SimpleNamespace(
            manager_mode=True,
            get_current_session=lambda **kwargs: ("default", SimpleNamespace(backend="codex", model="", workdir="")),
            sessions={"default": SimpleNamespace(backend="codex", model="", workdir="")},
        )
        fake_dashboard = SimpleNamespace(
            snapshot=SimpleNamespace(bridge_running=True, hub_running=True),
            bridge_conversations={"sender-a": binding_a},
            hub_state=SimpleNamespace(
                agents=[],
                tasks=[
                    SimpleNamespace(
                        id="task-hello",
                        sender_id="sender-a",
                        session_name="default",
                        source="wechat",
                        created_at="2026-04-21T10:00:00",
                        status="succeeded",
                        output="你好。",
                        error="",
                        prompt="你好",
                    ),
                ],
            ),
        )
        fake_config = SimpleNamespace(backend_id="main", default_backend="codex", active_account_id="wechat-bot")
        fake_agent = SimpleNamespace(id="main", backend="codex", model="gpt-5.4", workdir="/tmp/project")
        with patch("core.mcp_service.BridgeConfig.load", return_value=fake_config):
            with patch("core.mcp_service.HubConfig.load", return_value=SimpleNamespace(agents=[fake_agent])):
                with patch("core.mcp_service.load_dashboard_state", return_value=fake_dashboard):
                    result = get_management_snapshot("sender-a")
        self.assertTrue(result.ok)
        self.assertIn("该会话最后回复 结果：你好。", result.summary)
        self.assertNotIn("管理助手最后回复：", result.summary)

    def test_management_snapshot_uses_latest_completed_manager_reply_instead_of_current_running_request(self) -> None:
        binding_a = SimpleNamespace(
            manager_mode=True,
            get_current_session=lambda **kwargs: ("default", SimpleNamespace(backend="codex", model="", workdir="")),
            sessions={"default": SimpleNamespace(backend="codex", model="", workdir="")},
        )
        fake_dashboard = SimpleNamespace(
            snapshot=SimpleNamespace(bridge_running=True, hub_running=True),
            bridge_conversations={"sender-a": binding_a},
            hub_state=SimpleNamespace(
                agents=[],
                tasks=[
                    SimpleNamespace(
                        id="task-old",
                        sender_id="sender-a",
                        session_name="__manager__-sender-a",
                        source="wechat-manager",
                        created_at="2026-04-21T10:00:00",
                        status="succeeded",
                        output="你的业务会话（不含管理助手）：当前会话 `default`，共 `1` 个会话",
                        error="",
                        prompt="列出所有会话",
                    ),
                    SimpleNamespace(
                        id="task-current",
                        sender_id="sender-a",
                        session_name="__manager__-sender-a",
                        source="wechat-manager",
                        created_at="2026-04-21T10:01:00",
                        status="running",
                        output="",
                        error="",
                        prompt="列出所有会话",
                    ),
                ],
            ),
        )
        fake_config = SimpleNamespace(backend_id="main", default_backend="codex", active_account_id="wechat-bot")
        fake_agent = SimpleNamespace(id="main", backend="codex", model="gpt-5.4", workdir="/tmp/project")
        with patch("core.mcp_service.BridgeConfig.load", return_value=fake_config):
            with patch("core.mcp_service.HubConfig.load", return_value=SimpleNamespace(agents=[fake_agent])):
                with patch("core.mcp_service.load_dashboard_state", return_value=fake_dashboard):
                    result = get_management_snapshot("sender-a")
        self.assertTrue(result.ok)
        self.assertEqual("最近回复：已返回会话总览", result.data["target_sender"]["latest_manager_reply_summary"])
        self.assertNotIn("管理助手最后回复：", result.summary)
        self.assertNotIn("处理中：列出所有会话", result.summary)

    def test_list_sender_conversations_returns_global_sender_summary(self) -> None:
        binding_a = SimpleNamespace(
            get_current_session=lambda **kwargs: ("default", SimpleNamespace(backend="codex", model="", workdir="")),
            sessions={"default": SimpleNamespace(backend="codex", model="", workdir="")},
        )
        binding_b = SimpleNamespace(
            get_current_session=lambda **kwargs: ("deep-dive", SimpleNamespace(backend="claude", model="", workdir="")),
            sessions={"deep-dive": SimpleNamespace(backend="claude", model="", workdir="")},
        )
        fake_dashboard = SimpleNamespace(
            snapshot=SimpleNamespace(bridge_running=True, hub_running=True),
            bridge_conversations={"sender-a": binding_a, "sender-b": binding_b},
            hub_state=SimpleNamespace(
                agents=[],
                tasks=[
                    SimpleNamespace(id="task-m", sender_id="sender-a", session_name="__manager__-sender-a", source="wechat-manager", created_at="2026-04-21T08:59:00", status="succeeded", output="管理回复", error="", prompt="列出所有会话"),
                    SimpleNamespace(id="task-a", sender_id="sender-a", session_name="default", created_at="2026-04-21T09:00:00", status="succeeded", output="ok", error="", prompt="hello"),
                    SimpleNamespace(id="task-b", sender_id="sender-b", session_name="deep-dive", created_at="2026-04-21T09:01:00", status="failed", output="", error="boom", prompt="check"),
                ],
            ),
        )
        fake_config = SimpleNamespace(backend_id="main", default_backend="codex", active_account_id="wechat-bot")
        fake_agent = SimpleNamespace(id="main", backend="codex", model="gpt-5.4", workdir="/tmp/project")
        with patch("core.mcp_service.BridgeConfig.load", return_value=fake_config):
            with patch("core.mcp_service.HubConfig.load", return_value=SimpleNamespace(agents=[fake_agent])):
                with patch("core.mcp_service.load_dashboard_state", return_value=fake_dashboard):
                    result = list_sender_conversations(focus_sender_id="sender-a")
        self.assertTrue(result.ok)
        self.assertEqual(2, result.data["conversation_count"])
        self.assertEqual(2, len(result.data["senders"]))
        self.assertEqual("sender-a", result.data["senders"][0]["sender_id"])
        self.assertTrue(any("你的业务会话（不含管理助手）：" in line for line in result.data["summary_lines"]))
        self.assertIn("你的业务会话（不含管理助手）：当前会话 default，共 1 个会话", result.summary)
        self.assertEqual("最近回复：管理回复", result.data["senders"][0]["latest_manager_reply_summary"])
        self.assertTrue(any("其他会话来源 2 的业务会话（不含管理助手）：" in line for line in result.data["summary_lines"]))
        self.assertTrue(any("最近状态 已完成" in line for line in result.data["senders"][0]["summary_lines"]))
        self.assertTrue(any("最近状态 失败" in line for line in result.data["senders"][1]["summary_lines"]))
        self.assertTrue(any("该会话最后回复 结果：ok" in line for line in result.data["senders"][0]["summary_lines"]))
        self.assertFalse(any("任务 1" in line for line in result.data["senders"][0]["summary_lines"]))
