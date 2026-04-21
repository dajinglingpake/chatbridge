from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bridge_config import BridgeConfig
from core.state_models import IpcResponseEnvelope
from weixin_hub_bridge import WeixinBridge


class FakeBridge(WeixinBridge):
    def __init__(self, config: BridgeConfig) -> None:
        super().__init__(config)
        self.submit_payloads: list[dict[str, object]] = []
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
            self.submit_payloads.append(dict(payload))
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
            self.submit_payloads.append(dict(payload))
            return IpcResponseEnvelope(ok=True, payload={"task": {"id": "task-feedback-001"}})
        if action == "get_task":
            index = min(self._task_poll_index, len(self._task_states) - 1)
            self._task_poll_index += 1
            return IpcResponseEnvelope(ok=True, payload={"task": self._task_states[index]})
        return super()._ipc_request(action, payload, timeout_seconds)

    def _send_text(self, base_url: str, token: str, to_user_id: str, context_token, text: str) -> None:
        self.sent_texts.append(text)

    def poll_pending(self) -> None:
        self._poll_pending_tasks("https://example.com", "token")


class TimeoutPollingBridge(FakeBridge):
    def __init__(self, config: BridgeConfig) -> None:
        super().__init__(config)
        self.poll_calls = 0

    def _load_account(self) -> dict[str, object]:
        return {"token": "token", "baseUrl": "https://example.com"}

    def _load_sync_buf(self) -> str:
        return ""

    def _post_json(self, url: str, body: dict[str, object], token: str = "", timeout_ms: int = 15000) -> dict[str, object]:
        self.poll_calls += 1
        raise RuntimeError(f"POST {url} failed: The read operation timed out")


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
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        temp_root = Path(self._tempdir.name)
        self.conversation_path = temp_root / ".runtime" / "state" / "weixin_conversations.json"
        self.pending_tasks_path = temp_root / ".runtime" / "state" / "weixin_pending_tasks.json"
        self.event_log_path = temp_root / ".runtime" / "logs" / "weixin_bridge_events.jsonl"
        self.message_audit_log_path = temp_root / ".runtime" / "logs" / "weixin_bridge_message_audit.jsonl"
        self.state_path = temp_root / ".runtime" / "state" / "weixin_hub_bridge_state.json"
        self.conversation_path.parent.mkdir(parents=True, exist_ok=True)
        self.event_log_path.parent.mkdir(parents=True, exist_ok=True)
        self._patchers = [
            patch("weixin_hub_bridge.CONVERSATION_PATH", self.conversation_path),
            patch("weixin_hub_bridge.PENDING_TASKS_PATH", self.pending_tasks_path),
            patch("weixin_hub_bridge.EVENT_LOG_PATH", self.event_log_path),
            patch("weixin_hub_bridge.MESSAGE_AUDIT_LOG_PATH", self.message_audit_log_path),
            patch("weixin_hub_bridge.STATE_PATH", self.state_path),
            patch("weixin_hub_bridge.load_account_context_tokens", return_value={}),
            patch("weixin_hub_bridge.save_account_context_tokens", return_value=None),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self._original_lang = os.environ.get("CHATBRIDGE_LANG")
        os.environ["CHATBRIDGE_LANG"] = "en-US"
        self.conversation_path.write_text(
            json.dumps(
                {
                    "sender-test": {
                        "current_session": "default",
                        "manager_mode": False,
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

    def test_help_command_uses_spaced_multiline_layout(self) -> None:
        reply, handled = self.bridge._handle_control_command("sender-test", "/help")
        self.assertTrue(handled)
        self.assertIn("Available commands:", reply)
        self.assertIn("\n\n/manage on", reply)
        self.assertIn("\n\nNormal messages:", reply)

    def test_poll_once_ignores_expected_getupdates_timeout(self) -> None:
        bridge = TimeoutPollingBridge(BridgeConfig.load())
        bridge.poll_once()
        self.assertEqual(1, bridge.poll_calls)
        self.assertEqual("", bridge.state.last_error)

    def test_run_clears_persisted_error_after_expected_timeout(self) -> None:
        bridge = TimeoutPollingBridge(BridgeConfig.load())
        bridge.state.set_error("stale error")

        class _StopLoop(BaseException):
            pass

        original_save_state = bridge._save_state

        def stop_after_clean_state() -> None:
            original_save_state()
            if bridge.state.last_error == "":
                raise _StopLoop()

        bridge._save_state = stop_after_clean_state  # type: ignore[method-assign]

        with self.assertRaises(_StopLoop):
            bridge.run()

        self.assertEqual("", bridge.state.last_error)

    def test_new_sender_defaults_to_manager_mode(self) -> None:
        binding = self.bridge._ensure_conversation("sender-new")
        self.assertTrue(binding.manager_mode)

    def test_manage_command_toggles_manager_mode(self) -> None:
        reply, handled = self.bridge._handle_control_command("sender-test", "/manage on")
        self.assertTrue(handled)
        self.assertIn("Management assistant entry enabled", reply)
        self.assertTrue(self.bridge.conversations["sender-test"].manager_mode)

        reply, handled = self.bridge._handle_control_command("sender-test", "/manage off")
        self.assertTrue(handled)
        self.assertIn("Direct session entry enabled", reply)
        self.assertFalse(self.bridge.conversations["sender-test"].manager_mode)

    def test_handle_message_routes_new_sender_to_management_agent(self) -> None:
        bridge = FeedbackBridge(BridgeConfig.load(), [])
        bridge._handle_message(
            "https://example.com",
            "token",
            {
                "message_type": 1,
                "from_user_id": "sender-manager",
                "context_token": "ctx",
                "item_list": [{"type": 1, "text_item": {"text": "列出所有会话"}}],
            },
        )
        self.assertEqual("wechat-manager", bridge.submit_payloads[-1]["source"])
        self.assertTrue(str(bridge.submit_payloads[-1]["session_name"]).startswith("__manager__-"))
        self.assertEqual([], bridge.sent_texts)
        audits = [json.loads(line) for line in self.message_audit_log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual("task_submission", audits[-1]["route"])
        self.assertTrue(audits[-1]["manager_mode"])
        self.assertEqual("wechat-manager", audits[-1]["source"])

    def test_bridge_defaults_management_feedback_to_chinese_when_auto_and_env_unset(self) -> None:
        os.environ.pop("CHATBRIDGE_LANG", None)
        bridge = FeedbackBridge(BridgeConfig.load(), [])
        bridge._handle_message(
            "https://example.com",
            "token",
            {
                "message_type": 1,
                "from_user_id": "sender-manager",
                "context_token": "ctx",
                "item_list": [{"type": 1, "text_item": {"text": "列出所有会话"}}],
            },
        )
        self.assertEqual("zh-CN", bridge.localizer.language)
        self.assertEqual([], bridge.sent_texts)

    def test_handle_message_falls_back_to_management_agent_for_unknown_prompt(self) -> None:
        bridge = FeedbackBridge(BridgeConfig.load(), [])
        bridge._handle_message(
            "https://example.com",
            "token",
            {
                "message_type": 1,
                "from_user_id": "sender-manager",
                "context_token": "ctx",
                "item_list": [{"type": 1, "text_item": {"text": "帮我梳理一下最近这几个会话的差异并给建议"}}],
            },
        )
        self.assertEqual("wechat-manager", bridge.submit_payloads[-1]["source"])
        self.assertTrue(str(bridge.submit_payloads[-1]["session_name"]).startswith("__manager__-"))

    def test_handle_message_routes_natural_language_session_switch_to_management_agent(self) -> None:
        bridge = FeedbackBridge(BridgeConfig.load(), [])
        bridge._handle_control_command("sender-test", "/manage on")
        bridge._handle_message(
            "https://example.com",
            "token",
            {
                "message_type": 1,
                "from_user_id": "sender-test",
                "context_token": "ctx",
                "item_list": [{"type": 1, "text_item": {"text": "切换到 deep-dive 会话"}}],
            },
        )
        self.assertEqual("wechat-manager", bridge.submit_payloads[-1]["source"])
        self.assertEqual("__manager__-sender-test", bridge.submit_payloads[-1]["session_name"])
        self.assertEqual("default", bridge.conversations["sender-test"].current_session)
        self.assertEqual([], bridge.sent_texts)

    def test_handle_message_audits_control_command_without_submitting_task(self) -> None:
        bridge = FeedbackBridge(BridgeConfig.load(), [])
        bridge._handle_message(
            "https://example.com",
            "token",
            {
                "message_type": 1,
                "from_user_id": "sender-test",
                "context_token": "ctx",
                "item_list": [{"type": 1, "text_item": {"text": "/manage off"}}],
            },
        )
        self.assertEqual([], bridge.submit_payloads)
        audits = [json.loads(line) for line in self.message_audit_log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual("control_command", audits[-1]["route"])
        self.assertEqual("/manage", audits[-1]["command"])

    def test_management_task_result_uses_plain_manager_reply(self) -> None:
        bridge = FeedbackBridge(
            BridgeConfig.load(),
            [
                {
                    "id": "task-feedback-001",
                    "sender_id": "sender-manager",
                    "session_name": "__manager__-sender-manager",
                    "status": "succeeded",
                    "agent_id": "main",
                    "agent_name": "default",
                    "backend": "codex",
                    "prompt": "列出所有会话",
                    "output": "找到 2 个会话：default, deep-dive",
                    "created_at": "2026-04-20T12:00:00",
                }
            ],
        )
        bridge._handle_message(
            "https://example.com",
            "token",
            {
                "message_type": 1,
                "from_user_id": "sender-manager",
                "context_token": "ctx",
                "item_list": [{"type": 1, "text_item": {"text": "帮我梳理一下最近这几个会话的差异并给建议"}}],
            },
        )
        self.assertEqual([], bridge.sent_texts)
        bridge.poll_pending()
        self.assertEqual("找到 2 个会话：default, deep-dive", bridge.sent_texts[-1])

    def test_supported_management_prompt_still_uses_management_agent(self) -> None:
        bridge = FeedbackBridge(BridgeConfig.load(), [])
        bridge._handle_message(
            "https://example.com",
            "token",
            {
                "message_type": 1,
                "from_user_id": "sender-manager",
                "context_token": "ctx",
                "item_list": [{"type": 1, "text_item": {"text": "列出所有会话"}}],
            },
        )
        self.assertEqual("wechat-manager", bridge.submit_payloads[-1]["source"])
        self.assertEqual([], bridge.sent_texts)

    def test_handle_message_sends_queued_and_running_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            event_log_path = Path(temp_dir) / "weixin_bridge_events.jsonl"
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
            with patch("weixin_hub_bridge.EVENT_LOG_PATH", event_log_path):
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
                self.assertEqual([], bridge.sent_texts)
                bridge.poll_pending()
                bridge.poll_pending()
            self.assertEqual(1, len(bridge.sent_texts))
            self.assertIn("Task completed", bridge.sent_texts[0])
            self.assertIn("Session ID: -", bridge.sent_texts[0])
            self.assertIn("Result:\nworld", bridge.sent_texts[0])
            entries = [json.loads(line) for line in event_log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(["accepted", "running", "succeeded"], [entry["event"] for entry in entries])
            self.assertEqual("task-feedback-001", entries[-1]["task_id"])
            self.assertEqual("default", entries[-1]["session_name"])

    def test_handle_message_sends_incremental_progress_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            event_log_path = Path(temp_dir) / "weixin_bridge_events.jsonl"
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
                        "progress_text": "正在分析仓库结构",
                        "progress_seq": 1,
                        "created_at": "2026-04-20T12:00:00",
                    },
                    {
                        "id": "task-feedback-001",
                        "sender_id": "sender-test",
                        "session_name": "default",
                        "status": "running",
                        "agent_id": "main",
                        "agent_name": "default",
                        "backend": "codex",
                        "prompt": "hello",
                        "progress_text": "正在生成修复方案",
                        "progress_seq": 2,
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
            with patch("weixin_hub_bridge.EVENT_LOG_PATH", event_log_path):
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
                bridge.poll_pending()
                bridge.poll_pending()
                bridge.poll_pending()
            self.assertIn("正在生成修复方案", bridge.sent_texts[0])
            self.assertIn("Task completed", bridge.sent_texts[1])
            entries = [json.loads(line) for line in event_log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(
                ["accepted", "running", "progress", "progress", "succeeded"],
                [entry["event"] for entry in entries],
            )
            self.assertEqual("正在生成修复方案", entries[-2]["result_preview"])

    def test_management_task_sends_incremental_progress_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            event_log_path = Path(temp_dir) / "weixin_bridge_events.jsonl"
            bridge = FeedbackBridge(
                BridgeConfig.load(),
                [
                    {
                        "id": "task-feedback-001",
                        "sender_id": "sender-manager",
                        "session_name": "__manager__-sender-manager",
                        "status": "running",
                        "agent_id": "main",
                        "agent_name": "default",
                        "backend": "codex",
                        "prompt": "列出所有会话",
                        "progress_text": "正在调用 get_management_snapshot",
                        "progress_seq": 1,
                        "created_at": "2026-04-20T12:00:00",
                    },
                    {
                        "id": "task-feedback-001",
                        "sender_id": "sender-manager",
                        "session_name": "__manager__-sender-manager",
                        "status": "succeeded",
                        "agent_id": "main",
                        "agent_name": "default",
                        "backend": "codex",
                        "prompt": "列出所有会话",
                        "output": "找到 2 个会话：default, deep-dive",
                        "created_at": "2026-04-20T12:00:00",
                    },
                ],
            )
            with patch("weixin_hub_bridge.EVENT_LOG_PATH", event_log_path):
                bridge._handle_message(
                    "https://example.com",
                    "token",
                    {
                        "message_type": 1,
                        "from_user_id": "sender-manager",
                        "context_token": "ctx",
                        "item_list": [{"type": 1, "text_item": {"text": "列出所有会话"}}],
                    },
                )
                bridge.poll_pending()
                bridge.poll_pending()
            self.assertEqual(["找到 2 个会话：default, deep-dive"], bridge.sent_texts)
            entries = [json.loads(line) for line in event_log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(
                ["accepted", "running", "progress", "succeeded"],
                [entry["event"] for entry in entries],
            )
            self.assertEqual("正在调用 get_management_snapshot", entries[-2]["result_preview"])

    def test_handle_message_failure_includes_retry_hint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            event_log_path = Path(temp_dir) / "weixin_bridge_events.jsonl"
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
            with patch("weixin_hub_bridge.EVENT_LOG_PATH", event_log_path):
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
                self.assertEqual([], bridge.sent_texts)
                bridge.poll_pending()
            self.assertIn("/retry task-feedback-001", bridge.sent_texts[-1])
            self.assertIn("Session ID: -", bridge.sent_texts[-1])
            entries = [json.loads(line) for line in event_log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual("failed", entries[-1]["event"])
            self.assertEqual("boom", entries[-1]["error"])

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
        self.assertEqual([], bridge.sent_texts)
        bridge.poll_pending()
        bridge.poll_pending()
        self.assertIn("task was canceled", bridge.sent_texts[-1])
        self.assertIn("/retry task-feedback-001", bridge.sent_texts[-1])

    def test_task_result_keeps_original_session_after_switch(self) -> None:
        bridge = FeedbackBridge(
            BridgeConfig.load(),
            [
                {
                    "id": "task-feedback-001",
                    "sender_id": "sender-test",
                    "session_name": "default",
                    "session_id": "sess-001",
                    "status": "succeeded",
                    "agent_id": "main",
                    "agent_name": "default",
                    "backend": "codex",
                    "prompt": "hello",
                    "output": "world",
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
        reply, handled = bridge._handle_control_command("sender-test", "/new deep-dive")
        self.assertTrue(handled)
        self.assertIn("deep-dive", reply)
        bridge.poll_pending()
        self.assertIn("Session: default", bridge.sent_texts[-1])
        self.assertIn("Session ID: sess-001", bridge.sent_texts[-1])

    def test_events_command_returns_recent_async_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            event_log_path = Path(temp_dir) / "weixin_bridge_events.jsonl"
            event_log_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "at": "2026-04-20T12:00:00",
                                "event": "accepted",
                                "task_id": "task-a",
                                "sender_id": "sender-test",
                                "session_name": "default",
                                "session_id": "",
                                "backend": "codex",
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "at": "2026-04-20T12:00:05",
                                "event": "succeeded",
                                "task_id": "task-a",
                                "sender_id": "sender-test",
                                "session_name": "default",
                                "session_id": "sess-123",
                                "result_preview": "hello world",
                            },
                            ensure_ascii=False,
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with patch("weixin_hub_bridge.EVENT_LOG_PATH", event_log_path):
                reply, handled = self.bridge._handle_control_command("sender-test", "/events 2")
        self.assertTrue(handled)
        self.assertIn("Recent async events: 2/2", reply)
        self.assertIn("Completed", reply)
        self.assertIn("Submitted to codex", reply)
        self.assertIn("task-a", reply)
        self.assertIn("sess-123", reply)

    def test_events_command_defaults_to_chinese_event_names_when_auto_and_env_unset(self) -> None:
        os.environ.pop("CHATBRIDGE_LANG", None)
        bridge = FakeBridge(BridgeConfig.load())
        with tempfile.TemporaryDirectory() as temp_dir:
            event_log_path = Path(temp_dir) / "weixin_bridge_events.jsonl"
            event_log_path.write_text(
                json.dumps(
                    {
                        "at": "2026-04-20T12:00:00",
                        "event": "succeeded",
                        "task_id": "task-a",
                        "sender_id": "sender-test",
                        "session_name": "default",
                        "session_id": "sess-123",
                        "result_preview": "hello world",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            with patch("weixin_hub_bridge.EVENT_LOG_PATH", event_log_path):
                reply, handled = bridge._handle_control_command("sender-test", "/events 1")
        self.assertTrue(handled)
        self.assertIn("最近异步事件: 1/1", reply)
        self.assertIn("已完成", reply)

    def test_events_command_renders_human_detail_for_running_event(self) -> None:
        os.environ.pop("CHATBRIDGE_LANG", None)
        bridge = FakeBridge(BridgeConfig.load())
        with tempfile.TemporaryDirectory() as temp_dir:
            event_log_path = Path(temp_dir) / "weixin_bridge_events.jsonl"
            event_log_path.write_text(
                json.dumps(
                    {
                        "at": "2026-04-20T12:00:00",
                        "event": "running",
                        "task_id": "task-a",
                        "sender_id": "sender-test",
                        "session_name": "default",
                        "session_id": "",
                        "backend": "codex",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            with patch("weixin_hub_bridge.EVENT_LOG_PATH", event_log_path):
                reply, handled = bridge._handle_control_command("sender-test", "/events 1")
        self.assertTrue(handled)
        self.assertIn("处理中", reply)
        self.assertIn("正在由 codex 处理", reply)

    def test_events_command_renders_progress_event_detail(self) -> None:
        os.environ.pop("CHATBRIDGE_LANG", None)
        bridge = FakeBridge(BridgeConfig.load())
        with tempfile.TemporaryDirectory() as temp_dir:
            event_log_path = Path(temp_dir) / "weixin_bridge_events.jsonl"
            event_log_path.write_text(
                json.dumps(
                    {
                        "at": "2026-04-20T12:00:00",
                        "event": "progress",
                        "task_id": "task-a",
                        "sender_id": "sender-test",
                        "session_name": "default",
                        "session_id": "",
                        "result_preview": "正在调用 get_management_snapshot",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            with patch("weixin_hub_bridge.EVENT_LOG_PATH", event_log_path):
                reply, handled = bridge._handle_control_command("sender-test", "/events 1")
        self.assertTrue(handled)
        self.assertIn("进度更新", reply)
        self.assertIn("正在调用 get_management_snapshot", reply)

    def test_events_command_hides_legacy_global_sender_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            event_log_path = Path(temp_dir) / "weixin_bridge_events.jsonl"
            event_log_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "at": "2026-04-20T12:00:00",
                                "event": "accepted",
                                "task_id": "task-a",
                                "sender_id": "sender-test",
                                "session_name": "default",
                                "session_id": "",
                                "backend": "codex",
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "at": "2026-04-20T12:00:05",
                                "event": "succeeded",
                                "task_id": "task-a",
                                "sender_id": "sender-test",
                                "session_name": "__manager__-sender-test",
                                "session_id": "sess-123",
                                "result_preview": "会话总览：\n当前你: 当前会话 default\n\n发送方 2: 当前会话 deep-dive",
                            },
                            ensure_ascii=False,
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with patch("weixin_hub_bridge.EVENT_LOG_PATH", event_log_path):
                reply, handled = self.bridge._handle_control_command("sender-test", "/events 5")
        self.assertTrue(handled)
        self.assertIn("Recent async events: 1/5", reply)
        self.assertIn("task-a", reply)
        self.assertNotIn("发送方 2", reply)

    def test_task_lookup_command_returns_summary(self) -> None:
        reply, handled = self.bridge._handle_control_command("sender-test", "/task task-test-001")
        self.assertTrue(handled)
        self.assertIn("Task details", reply)
        self.assertIn("Task ID: task-test-001", reply)
        self.assertIn("Prompt summary:", reply)
        self.assertIn("Result summary:", reply)

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
        self.assertIn("Status: Failed", reply)

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
        self.assertIn("Current relation:", reply)
        self.assertIn("Assistant main", reply)
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
        self.assertIn("Current setup", reply)
        self.assertIn("Assistant default model: gpt-5.4", reply)
        self.assertIn("Current model: gpt-5.4", reply)
        self.assertIn("Assistant default directory: /tmp/project-alpha", reply)
        self.assertIn("Note: session backend/model/directory overrides win", reply)

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
        self.assertIn("Available assistants:", reply)
        self.assertIn("/tmp/project-alpha", reply)
        self.assertIn("reviewer | claude", reply)

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
