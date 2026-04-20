from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bridge_config import BridgeConfig
from core.state_models import IpcResponseEnvelope
from runtime_stack import BRIDGE_CONVERSATIONS_PATH
from weixin_hub_bridge import WeixinBridge


class FakeBridge(WeixinBridge):
    def __init__(self, config: BridgeConfig) -> None:
        super().__init__(config)
        self._state_payload = IpcResponseEnvelope(
            ok=True,
            payload={
                "tasks": [
                    {
                        "id": "task-test-000",
                        "sender_id": "sender-test",
                        "session_name": "default",
                        "status": "queued",
                        "agent_id": "main",
                        "agent_name": "default",
                        "backend": "codex",
                        "prompt": "queued follow-up",
                        "created_at": "2026-04-20T09:00:00",
                    },
                    {
                        "id": "task-test-001",
                        "sender_id": "sender-test",
                        "session_name": "default",
                        "status": "succeeded",
                        "agent_id": "main",
                        "agent_name": "default",
                        "backend": "codex",
                        "prompt": "hello",
                        "output": "world",
                        "created_at": "2026-04-20T10:00:00",
                        "finished_at": "2026-04-20T10:01:00",
                    },
                    {
                        "id": "task-test-002",
                        "sender_id": "sender-test",
                        "session_name": "deep-dive",
                        "status": "failed",
                        "agent_id": "main",
                        "agent_name": "default",
                        "backend": "claude",
                        "prompt": "investigate bug",
                        "error": "stacktrace details",
                        "created_at": "2026-04-20T11:00:00",
                        "finished_at": "2026-04-20T11:02:00",
                    },
                ],
            },
        )
    def _ipc_request(self, action: str, payload: dict[str, object], timeout_seconds: float) -> IpcResponseEnvelope:
        if action == "state":
            return self._state_payload
        if action == "get_task":
            task_id = str(payload.get("task_id") or "")
            for task in self._state_payload.payload["tasks"]:
                if str(task.get("id") or "") == task_id:
                    return IpcResponseEnvelope(ok=True, payload={"task": task})
            return IpcResponseEnvelope(ok=False, error="task not found")
        if action == "cancel_task":
            task_id = str(payload.get("task_id") or "")
            for task in self._state_payload.payload["tasks"]:
                if str(task.get("id") or "") != task_id:
                    continue
                if str(task.get("status") or "") in {"queued", "running"}:
                    task["status"] = "canceled"
                    task["finished_at"] = "2026-04-20T12:00:00"
                    task["error"] = (
                        "Task canceled before execution."
                        if str(task.get("started_at") or "") == ""
                        else "Task canceled during execution."
                    )
                    return IpcResponseEnvelope(ok=True, payload={"task": task})
                return IpcResponseEnvelope(ok=False, error=f"task cannot be canceled from status: {task.get('status')}")
            return IpcResponseEnvelope(ok=False, error="task not found")
        if action == "retry_task":
            task_id = str(payload.get("task_id") or "")
            for task in self._state_payload.payload["tasks"]:
                if str(task.get("id") or "") != task_id:
                    continue
                retried_task = {
                    **task,
                    "id": "task-retry-001",
                    "status": "queued",
                    "source": str(payload.get("source") or "wechat"),
                    "sender_id": str(payload.get("sender_id") or task.get("sender_id") or ""),
                    "started_at": "",
                    "finished_at": "",
                    "output": "",
                    "error": "",
                    "created_at": "2026-04-20T12:05:00",
                }
                self._state_payload.payload["tasks"].append(retried_task)
                return IpcResponseEnvelope(ok=True, payload={"task": retried_task})
            return IpcResponseEnvelope(ok=False, error="task not found")
        if action == "submit_task":
            return IpcResponseEnvelope(ok=True, payload={"task": {"id": "task-forwarded-001"}})
        raise RuntimeError(f"unexpected action: {action}")

    def _save_conversations(self) -> None:
        return None


class FeedbackBridge(FakeBridge):
    def __init__(self, config: BridgeConfig, task_states: list[dict[str, object]]) -> None:
        self.sent_texts: list[str] = []
        self._task_states = task_states
        self._task_poll_index = 0
        super().__init__(config)

    def _ipc_request(self, action: str, payload: dict[str, object], timeout_seconds: float) -> IpcResponseEnvelope:
        if action == "submit_task":
            return IpcResponseEnvelope(ok=True, payload={"task": {"id": "task-feedback-001"}})
        if action == "get_task":
            index = min(self._task_poll_index, len(self._task_states) - 1)
            self._task_poll_index += 1
            return IpcResponseEnvelope(ok=True, payload={"task": self._task_states[index]})
        return super()._ipc_request(action, payload, timeout_seconds)

    def _send_text(self, base_url: str, token: str, to_user_id: str, context_token, text: str) -> None:
        self.sent_texts.append(text)


def _fake_agent(
    agent_id: str,
    *,
    backend: str,
    model: str,
    workdir: str,
    name: str | None = None,
    enabled: bool = True,
):
    return SimpleNamespace(
        id=agent_id,
        name=name or agent_id,
        backend=backend,
        model=model,
        workdir=workdir,
        enabled=enabled,
    )


class WeixinBridgeCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_lang = os.environ.get("CHATBRIDGE_LANG")
        os.environ["CHATBRIDGE_LANG"] = "en-US"
        BRIDGE_CONVERSATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        BRIDGE_CONVERSATIONS_PATH.write_text(
            json.dumps(
                {
                    "sender-test": {
                        "current_session": "default",
                        "sessions": {
                            "default": {"backend": "codex"},
                            "deep-dive": {"backend": "claude"},
                            "zzz-empty": {"backend": "opencode", "updated_at": "2026-04-19T09:00:00"},
                        },
                    }
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self.bridge = FakeBridge(BridgeConfig.load())

    def tearDown(self) -> None:
        if self._original_lang is None:
            os.environ.pop("CHATBRIDGE_LANG", None)
        else:
            os.environ["CHATBRIDGE_LANG"] = self._original_lang

    def test_notify_command_renders_multiline_status(self) -> None:
        reply, handled = self.bridge._handle_control_command("sender-test", "/notify")
        self.assertTrue(handled)
        self.assertIn("Current system notices", reply)
        self.assertIn("Service lifecycle:", reply)
        self.assertNotIn("\\n", reply)

    def test_handle_message_sends_queued_and_running_feedback(self) -> None:
        bridge = FeedbackBridge(
            BridgeConfig.load(),
            [
                {
                    "id": "task-feedback-001",
                    "sender_id": "sender-test",
                    "session_name": "default",
                    "status": "running",
                    "agent_id": "main",
                    "agent_name": "default",
                    "backend": "codex",
                    "prompt": "hello",
                    "created_at": "2026-04-20T12:00:00",
                },
                {
                    "id": "task-feedback-001",
                    "sender_id": "sender-test",
                    "session_name": "default",
                    "status": "succeeded",
                    "agent_id": "main",
                    "agent_name": "default",
                    "backend": "codex",
                    "prompt": "hello",
                    "output": "world",
                    "created_at": "2026-04-20T12:00:00",
                },
            ],
        )
        bridge._handle_message(
            "https://example.com",
            "token",
            {
                "message_type": 1,
                "from_user_id": "sender-test",
                "context_token": "ctx",
                "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
            },
        )
        self.assertIn("Task accepted", bridge.sent_texts[0])
        self.assertIn("Task is now running", bridge.sent_texts[1])
        self.assertEqual("world", bridge.sent_texts[2])

    def test_handle_message_failure_includes_retry_hint(self) -> None:
        bridge = FeedbackBridge(
            BridgeConfig.load(),
            [
                {
                    "id": "task-feedback-001",
                    "sender_id": "sender-test",
                    "session_name": "default",
                    "status": "failed",
                    "agent_id": "main",
                    "agent_name": "default",
                    "backend": "codex",
                    "prompt": "hello",
                    "error": "boom",
                    "created_at": "2026-04-20T12:00:00",
                }
            ],
        )
        bridge._handle_message(
            "https://example.com",
            "token",
            {
                "message_type": 1,
                "from_user_id": "sender-test",
                "context_token": "ctx",
                "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
            },
        )
        self.assertIn("Task accepted", bridge.sent_texts[0])
        self.assertIn("/retry task-feedback-001", bridge.sent_texts[-1])

    def test_handle_message_canceled_includes_retry_hint(self) -> None:
        bridge = FeedbackBridge(
            BridgeConfig.load(),
            [
                {
                    "id": "task-feedback-001",
                    "sender_id": "sender-test",
                    "session_name": "default",
                    "status": "running",
                    "agent_id": "main",
                    "agent_name": "default",
                    "backend": "codex",
                    "prompt": "hello",
                    "created_at": "2026-04-20T12:00:00",
                },
                {
                    "id": "task-feedback-001",
                    "sender_id": "sender-test",
                    "session_name": "default",
                    "status": "canceled",
                    "agent_id": "main",
                    "agent_name": "default",
                    "backend": "codex",
                    "prompt": "hello",
                    "error": "Task canceled during execution.",
                    "created_at": "2026-04-20T12:00:00",
                },
            ],
        )
        bridge._handle_message(
            "https://example.com",
            "token",
            {
                "message_type": 1,
                "from_user_id": "sender-test",
                "context_token": "ctx",
                "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
            },
        )
        self.assertIn("Task accepted", bridge.sent_texts[0])
        self.assertIn("Task is now running", bridge.sent_texts[1])
        self.assertIn("task was canceled", bridge.sent_texts[-1])
        self.assertIn("/retry task-feedback-001", bridge.sent_texts[-1])

    def test_task_lookup_command_returns_summary(self) -> None:
        reply, handled = self.bridge._handle_control_command("sender-test", "/task task-test-001")
        self.assertTrue(handled)
        self.assertIn("Task ID: task-test-001", reply)
        self.assertIn("Prompt summary:", reply)
        self.assertIn("Output/Error summary:", reply)

    def test_cancel_command_cancels_queued_task(self) -> None:
        reply, handled = self.bridge._handle_control_command("sender-test", "/cancel task-test-000")
        self.assertTrue(handled)
        self.assertIn("Canceled task: task-test-000", reply)
        matching = next(task for task in self.bridge._state_payload.payload["tasks"] if task["id"] == "task-test-000")
        self.assertEqual("canceled", matching["status"])

    def test_cancel_command_cancels_running_task(self) -> None:
        self.bridge._state_payload.payload["tasks"].append(
            {
                "id": "task-test-003",
                "sender_id": "sender-test",
                "session_name": "default",
                "status": "running",
                "agent_id": "main",
                "agent_name": "default",
                "backend": "codex",
                "prompt": "long running work",
                "created_at": "2026-04-20T12:10:00",
                "started_at": "2026-04-20T12:11:00",
            }
        )
        reply, handled = self.bridge._handle_control_command("sender-test", "/cancel task-test-003")
        self.assertTrue(handled)
        self.assertIn("Canceled task: task-test-003", reply)
        matching = next(task for task in self.bridge._state_payload.payload["tasks"] if task["id"] == "task-test-003")
        self.assertEqual("canceled", matching["status"])

    def test_retry_command_requeues_latest_sender_task(self) -> None:
        reply, handled = self.bridge._handle_control_command("sender-test", "/retry task-test-002")
        self.assertTrue(handled)
        self.assertIn("Original: task-test-002", reply)
        self.assertIn("New task: task-retry-001", reply)

    def test_last_command_returns_latest_sender_task(self) -> None:
        reply, handled = self.bridge._handle_control_command("sender-test", "/last")
        self.assertTrue(handled)
        self.assertIn("Task ID: task-test-002", reply)
        self.assertIn("Status: failed", reply)

    def test_backend_switch_command_updates_current_session_backend(self) -> None:
        reply, handled = self.bridge._handle_control_command("sender-test", "/backend claude")
        self.assertTrue(handled)
        self.assertIn("claude", reply)
        binding = self.bridge.conversations["sender-test"]
        self.assertEqual(binding.sessions["default"].backend, "claude")

    def test_context_command_explains_runtime_relations(self) -> None:
        fake_hub_config = SimpleNamespace(
            agents=[
                _fake_agent(
                    "main",
                    backend="codex",
                    model="gpt-5.4",
                    workdir="/tmp/project-alpha",
                    name="Main",
                )
            ]
        )
        with patch("weixin_hub_bridge.HubConfig.load", return_value=fake_hub_config):
            reply, handled = self.bridge._handle_control_command("sender-test", "/context")
        self.assertTrue(handled)
        self.assertIn("Context relations:", reply)
        self.assertIn("Agent main", reply)
        self.assertIn("Session default", reply)

    def test_model_status_uses_agent_default_model(self) -> None:
        fake_hub_config = SimpleNamespace(
            agents=[_fake_agent("main", backend="codex", model="gpt-5.4", workdir="/tmp/project-alpha", name="Main")]
        )
        with patch("weixin_hub_bridge.HubConfig.load", return_value=fake_hub_config):
            reply, handled = self.bridge._handle_control_command("sender-test", "/model")
        self.assertTrue(handled)
        self.assertIn("Current model: gpt-5.4", reply)

    def test_model_switch_updates_current_session_model(self) -> None:
        reply, handled = self.bridge._handle_control_command("sender-test", "/model gpt-5.5")
        self.assertTrue(handled)
        self.assertIn("Current model: gpt-5.5", reply)
        binding = self.bridge.conversations["sender-test"]
        self.assertEqual("gpt-5.5", binding.sessions["default"].model)

    def test_model_reset_clears_session_override(self) -> None:
        self.bridge._handle_control_command("sender-test", "/model gpt-5.5")
        reply, handled = self.bridge._handle_control_command("sender-test", "/model reset")
        self.assertTrue(handled)
        self.assertIn("Reverted to the agent default model", reply)
        binding = self.bridge.conversations["sender-test"]
        self.assertEqual("", binding.sessions["default"].model)

    def test_project_status_uses_agent_default_workdir(self) -> None:
        fake_hub_config = SimpleNamespace(
            agents=[_fake_agent("main", backend="codex", model="", workdir="/tmp/project-alpha", name="Main")]
        )
        with patch("weixin_hub_bridge.HubConfig.load", return_value=fake_hub_config):
            reply, handled = self.bridge._handle_control_command("sender-test", "/project")
        self.assertTrue(handled)
        self.assertIn("Current project directory: /tmp/project-alpha", reply)

    def test_project_switch_updates_current_session_workdir(self) -> None:
        project_dir = Path("/home/dajingling/PythonProjects/chatbridge/workspace/project-gamma")
        project_dir.mkdir(parents=True, exist_ok=True)
        reply, handled = self.bridge._handle_control_command("sender-test", "/project project-gamma")
        self.assertTrue(handled)
        self.assertIn(str(project_dir), reply)
        binding = self.bridge.conversations["sender-test"]
        self.assertEqual(str(project_dir), binding.sessions["default"].workdir)

    def test_project_reset_clears_session_override(self) -> None:
        project_dir = Path("/home/dajingling/PythonProjects/chatbridge/workspace/project-epsilon")
        project_dir.mkdir(parents=True, exist_ok=True)
        self.bridge._handle_control_command("sender-test", "/project project-epsilon")
        reply, handled = self.bridge._handle_control_command("sender-test", "/project reset")
        self.assertTrue(handled)
        self.assertIn("Reverted to the agent default directory", reply)
        binding = self.bridge.conversations["sender-test"]
        self.assertEqual("", binding.sessions["default"].workdir)

    def test_project_switch_rejects_unknown_directory(self) -> None:
        reply, handled = self.bridge._handle_control_command("sender-test", "/project missing-project")
        self.assertTrue(handled)
        self.assertIn("Project directory not found: missing-project", reply)

    def test_project_list_shows_available_directories(self) -> None:
        project_dir = Path("/home/dajingling/PythonProjects/chatbridge/workspace/project-delta")
        project_dir.mkdir(parents=True, exist_ok=True)
        fake_hub_config = SimpleNamespace(
            agents=[_fake_agent("main", backend="codex", model="", workdir="/tmp/project-alpha", name="Main")]
        )
        with patch("weixin_hub_bridge.HubConfig.load", return_value=fake_hub_config):
            reply, handled = self.bridge._handle_control_command("sender-test", "/project list")
        self.assertTrue(handled)
        self.assertIn("Available project directories:", reply)
        self.assertIn("project-delta", reply)

    def test_list_command_includes_session_summary(self) -> None:
        reply, handled = self.bridge._handle_control_command("sender-test", "/list")
        self.assertTrue(handled)
        self.assertIn("Sessions:", reply)
        self.assertIn("page 1/1", reply)
        self.assertIn("deep-dive [claude]", reply)
        self.assertIn("stacktrace details", reply)
        lines = reply.splitlines()
        self.assertIn("deep-dive [claude]", lines[1])
        self.assertIn("default [codex]", lines[2])
        self.assertIn("zzz-empty [opencode]", lines[3])

    def test_sessions_command_supports_pagination(self) -> None:
        for index in range(6):
            session_name = f"bulk-{index}"
            self.bridge.conversations["sender-test"].sessions[session_name] = self.bridge._new_session_meta("codex")
        reply, handled = self.bridge._handle_control_command("sender-test", "/sessions 2")
        self.assertTrue(handled)
        self.assertIn("page 2/2", reply)

    def test_sessions_search_filters_by_name(self) -> None:
        reply, handled = self.bridge._handle_control_command("sender-test", "/sessions search deep")
        self.assertTrue(handled)
        self.assertIn("deep-dive [claude]", reply)
        self.assertNotIn("default [codex]", reply)

    def test_sessions_delete_removes_multiple_targets(self) -> None:
        reply, handled = self.bridge._handle_control_command("sender-test", "/sessions delete deep-dive,zzz-empty")
        self.assertTrue(handled)
        self.assertIn("Deleted: deep-dive, zzz-empty", reply)
        binding = self.bridge.conversations["sender-test"]
        self.assertNotIn("deep-dive", binding.sessions)
        self.assertNotIn("zzz-empty", binding.sessions)

    def test_sessions_clear_empty_removes_empty_sessions(self) -> None:
        reply, handled = self.bridge._handle_control_command("sender-test", "/sessions clear-empty")
        self.assertTrue(handled)
        self.assertIn("Deleted: zzz-empty", reply)
        binding = self.bridge.conversations["sender-test"]
        self.assertNotIn("zzz-empty", binding.sessions)

    def test_preview_command_returns_recent_rounds(self) -> None:
        reply, handled = self.bridge._handle_control_command("sender-test", "/preview deep-dive")
        self.assertTrue(handled)
        self.assertIn("Session preview: deep-dive", reply)
        self.assertIn("User: investigate bug", reply)
        self.assertIn("Error: stacktrace details", reply)

    def test_history_command_returns_session_summary(self) -> None:
        reply, handled = self.bridge._handle_control_command("sender-test", "/history deep-dive")
        self.assertTrue(handled)
        self.assertIn("Session history: deep-dive", reply)
        self.assertIn("History summary:", reply)
        self.assertIn("task-test-002", reply)

    def test_export_command_writes_markdown_file(self) -> None:
        reply, handled = self.bridge._handle_control_command("sender-test", "/export deep-dive")
        self.assertTrue(handled)
        self.assertIn("Session history exported", reply)
        export_path = Path("/home/dajingling/PythonProjects/chatbridge/.runtime/exports/sender-test__deep-dive.md")
        self.assertTrue(export_path.exists())
        content = export_path.read_text(encoding="utf-8")
        self.assertIn("# Session Export: deep-dive", content)
        self.assertIn("investigate bug", content)

    def test_rename_command_updates_current_session(self) -> None:
        reply, handled = self.bridge._handle_control_command("sender-test", "/use deep-dive")
        self.assertTrue(handled)
        reply, handled = self.bridge._handle_control_command("sender-test", "/rename bugfix")
        self.assertTrue(handled)
        self.assertIn("deep-dive -> bugfix", reply)
        binding = self.bridge.conversations["sender-test"]
        self.assertEqual(binding.current_session, "bugfix")
        self.assertIn("bugfix", binding.sessions)
        self.assertNotIn("deep-dive", binding.sessions)

    def test_delete_command_removes_named_session(self) -> None:
        reply, handled = self.bridge._handle_control_command("sender-test", "/delete deep-dive")
        self.assertTrue(handled)
        self.assertIn("Deleted session: deep-dive", reply)
        binding = self.bridge.conversations["sender-test"]
        self.assertNotIn("deep-dive", binding.sessions)

    def test_delete_current_session_switches_back_to_default(self) -> None:
        reply, handled = self.bridge._handle_control_command("sender-test", "/use deep-dive")
        self.assertTrue(handled)
        reply, handled = self.bridge._handle_control_command("sender-test", "/delete deep-dive")
        self.assertTrue(handled)
        self.assertIn("Current session: default", reply)
        binding = self.bridge.conversations["sender-test"]
        self.assertEqual(binding.current_session, "default")

    def test_status_command_includes_model_and_workdir(self) -> None:
        fake_hub_config = SimpleNamespace(
            agents=[
                _fake_agent(
                    "main",
                    backend="codex",
                    model="gpt-5.4",
                    workdir="/tmp/project-alpha",
                    name="Main",
                )
            ]
        )
        with patch("weixin_hub_bridge.HubConfig.load", return_value=fake_hub_config):
            reply, handled = self.bridge._handle_control_command("sender-test", "/status")
        self.assertTrue(handled)
        self.assertIn("Agent model: gpt-5.4", reply)
        self.assertIn("Current model: gpt-5.4", reply)
        self.assertIn("Agent workdir: /tmp/project-alpha", reply)
        self.assertIn("Relation: session backend/model/project overrides win", reply)

    def test_status_command_prefers_session_model_override(self) -> None:
        self.bridge._handle_control_command("sender-test", "/model gpt-5.5")
        fake_hub_config = SimpleNamespace(
            agents=[
                _fake_agent(
                    "main",
                    backend="codex",
                    model="gpt-5.4",
                    workdir="/tmp/project-alpha",
                    name="Main",
                )
            ]
        )
        with patch("weixin_hub_bridge.HubConfig.load", return_value=fake_hub_config):
            reply, handled = self.bridge._handle_control_command("sender-test", "/status")
        self.assertTrue(handled)
        self.assertIn("Current model: gpt-5.5", reply)

    def test_agent_list_command_shows_workdir_and_model(self) -> None:
        fake_hub_config = SimpleNamespace(
            agents=[
                _fake_agent("main", backend="codex", model="gpt-5.4", workdir="/tmp/project-alpha", name="Main"),
                _fake_agent("reviewer", backend="claude", model="", workdir="/tmp/project-beta", name="Reviewer"),
            ]
        )
        with patch("weixin_hub_bridge.HubConfig.load", return_value=fake_hub_config):
            reply, handled = self.bridge._handle_control_command("sender-test", "/agent list")
        self.assertTrue(handled)
        self.assertIn("Agents:", reply)
        self.assertIn("/tmp/project-alpha", reply)
        self.assertIn("reviewer | Backend: claude", reply)

    def test_agent_help_command_shows_codex_command_tree(self) -> None:
        fake_hub_config = SimpleNamespace(
            agents=[_fake_agent("main", backend="codex", model="", workdir="/tmp/project-alpha", name="Main")]
        )
        with patch("weixin_hub_bridge.HubConfig.load", return_value=fake_hub_config):
            reply, handled = self.bridge._handle_control_command("sender-test", "/agent help")
        self.assertTrue(handled)
        self.assertIn("codex exec", reply)
        self.assertIn("codex review", reply)
        self.assertIn("codex login status", reply)

    def test_agent_help_command_shows_claude_command_tree(self) -> None:
        fake_hub_config = SimpleNamespace(
            agents=[_fake_agent("main", backend="claude", model="", workdir="/tmp/project-alpha", name="Main")]
        )
        with patch("weixin_hub_bridge.HubConfig.load", return_value=fake_hub_config):
            reply, handled = self.bridge._handle_control_command("sender-test", "/agent help")
        self.assertTrue(handled)
        self.assertIn("Claude Code CLI", reply)
        self.assertIn("claude -p", reply)
        self.assertIn("claude agents", reply)

    def test_agent_help_command_shows_opencode_command_tree(self) -> None:
        fake_hub_config = SimpleNamespace(
            agents=[_fake_agent("main", backend="opencode", model="", workdir="/tmp/project-alpha", name="Main")]
        )
        with patch("weixin_hub_bridge.HubConfig.load", return_value=fake_hub_config):
            reply, handled = self.bridge._handle_control_command("sender-test", "/agent help")
        self.assertTrue(handled)
        self.assertIn("OpenCode CLI", reply)
        self.assertIn("opencode run", reply)
        self.assertIn("opencode session", reply)

    def test_extract_passthrough_prompt_strips_one_slash(self) -> None:
        self.assertEqual(self.bridge._extract_passthrough_prompt("//status"), "/status")
        self.assertEqual(self.bridge._extract_passthrough_prompt("///help"), "//help")
        self.assertIsNone(self.bridge._extract_passthrough_prompt("/status"))


if __name__ == "__main__":
    unittest.main()
