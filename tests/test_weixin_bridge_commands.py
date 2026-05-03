from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bridge_config import BridgeConfig
from core.state_models import IpcResponseEnvelope, WeixinPendingTaskState
from core.weixin_message_format import format_weixin_reply, prefix_weixin_output
from weixin_hub_bridge import WeixinBridge


class FakeBridge(WeixinBridge):
    def __init__(self, config: BridgeConfig) -> None:
        super().__init__(config)
        self.submit_payloads: list[dict[str, object]] = []
        self.codex_status_response = IpcResponseEnvelope(
            ok=True,
            payload={
                "status": (
                    "OpenAI Codex v0.122.0\n"
                    "\n"
                    "Model: gpt-5.4 (reasoning high, fast)\n"
                    "\n"
                    "Rate limits: unavailable"
                )
            },
        )
        self.task_context_left_response = IpcResponseEnvelope(ok=True, payload={"context_left_percent": None})
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
        if action == "codex_status":
            return self.codex_status_response
        if action == "task_context_left":
            return self.task_context_left_response
        raise RuntimeError(f"unexpected action: {action}")

    def _save_conversations(self) -> None:
        return None


class FeedbackBridge(FakeBridge):
    def __init__(self, config: BridgeConfig, task_states: list[dict[str, object]]) -> None:
        self.sent_texts: list[str] = []
        self._task_states = task_states
        self._task_update_index = 0
        super().__init__(config)

    def _ipc_request(self, action: str, payload: dict[str, object], timeout_seconds: float) -> IpcResponseEnvelope:
        if action == "submit_task":
            self.submit_payloads.append(dict(payload))
            return IpcResponseEnvelope(ok=True, payload={"task": {"id": "task-feedback-001"}})
        if action == "get_task":
            index = min(self._task_update_index, len(self._task_states) - 1)
            self._task_update_index += 1
            return IpcResponseEnvelope(ok=True, payload={"task": self._task_states[index]})
        return super()._ipc_request(action, payload, timeout_seconds)

    def _send_text(self, base_url: str, token: str, to_user_id: str, context_token, text: str) -> None:
        self.sent_texts.append(text)

    def _deliver_text_now(self, base_url: str, token: str, to_user_id: str, context_token, text: str) -> None:
        self.sent_texts.append(text)

    def _post_json(self, url: str, body: dict[str, object], token: str = "", timeout_ms: int = 15000) -> dict[str, object]:
        if url.endswith("/ilink/bot/getconfig"):
            return {"ret": 0, "typing_ticket": "ticket-feedback"}
        if url.endswith("/ilink/bot/sendtyping"):
            return {"ret": 0}
        raise RuntimeError(f"unexpected url: {url}")

    def process_next_pushed_update(self) -> None:
        if not self.pending_tasks:
            return
        task_id = next(iter(self.pending_tasks))
        data = self._ipc_request("get_task", {"task_id": task_id}, timeout_seconds=5)
        if data.ok:
            self._handle_pushed_task_update("https://example.com", "token", {"event": "task_update", "task": data.payload.get("task")})


class TypingBridge(FeedbackBridge):
    def __init__(self, config: BridgeConfig, task_states: list[dict[str, object]]) -> None:
        self.typing_calls: list[tuple[str, dict[str, object]]] = []
        super().__init__(config, task_states)

    def _post_json(self, url: str, body: dict[str, object], token: str = "", timeout_ms: int = 15000) -> dict[str, object]:
        if url.endswith("/ilink/bot/getconfig"):
            self.typing_calls.append(("getconfig", dict(body)))
            return {"ret": 0, "typing_ticket": "ticket-typing"}
        if url.endswith("/ilink/bot/sendtyping"):
            self.typing_calls.append(("sendtyping", dict(body)))
            return {"ret": 0}
        return super()._post_json(url, body, token=token, timeout_ms=timeout_ms)


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
    def test_format_bridge_reply_adds_compact_header(self) -> None:
        reply = format_weixin_reply("hello")
        self.assertTrue(reply.startswith("reply · - · "))
        self.assertIn("\n\nhello", reply)

    def test_format_bridge_reply_does_not_wrap_existing_header(self) -> None:
        output = prefix_weixin_output("running", "3s", "hello", at="2026-04-23T18:09:46")
        self.assertEqual(output, format_weixin_reply(output))

    def test_format_bridge_reply_preserves_context_window_header(self) -> None:
        output = prefix_weixin_output(
            "running",
            "3s",
            "hello",
            at="2026-04-23T18:09:46",
            context_left_percent=20,
        )
        self.assertEqual("running · 3s · ctx 20% · 18:09:46\n\nhello", output)
        self.assertEqual(output, format_weixin_reply(output))

    def test_format_retried_delivery_text_updates_header_only(self) -> None:
        original = "done · 10s · 18:09:46\n\nfinal output"
        retried = self.bridge._format_retried_delivery_text(original, 2)
        self.assertEqual(
            "done (resend=2) · 10s · 18:09:46\n\nfinal output",
            retried,
        )

    def test_format_retried_delivery_text_keeps_generic_reply_header_compact(self) -> None:
        original = format_weixin_reply("hello", at="2026-04-24T17:04:32")
        retried = self.bridge._format_retried_delivery_text(original, 5)
        self.assertEqual(
            "reply (resend=5) · - · 17:04:32\n\nhello",
            retried,
        )

    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        temp_root = Path(self._tempdir.name)
        self.conversation_path = temp_root / ".runtime" / "state" / "weixin_conversations.json"
        self.pending_tasks_path = temp_root / ".runtime" / "state" / "weixin_pending_tasks.json"
        self.project_spaces_path = temp_root / ".runtime" / "state" / "project_spaces.json"
        self.event_log_path = temp_root / ".runtime" / "logs" / "weixin_bridge_events.jsonl"
        self.message_audit_log_path = temp_root / ".runtime" / "logs" / "weixin_bridge_message_audit.jsonl"
        self.restart_notice_path = temp_root / ".runtime" / "state" / "weixin_restart_notice.json"
        self.service_action_state_path = temp_root / ".runtime" / "state" / "service_action_state.json"
        self.state_path = temp_root / ".runtime" / "state" / "weixin_hub_bridge_state.json"
        self.app_dir = temp_root / "app"
        self.export_dir = temp_root / ".runtime" / "exports"
        self.session_dir = temp_root / "sessions"
        self.conversation_path.parent.mkdir(parents=True, exist_ok=True)
        self.event_log_path.parent.mkdir(parents=True, exist_ok=True)
        self.app_dir.mkdir(parents=True, exist_ok=True)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._patchers = [
            patch("weixin_hub_bridge.APP_DIR", self.app_dir),
            patch("weixin_hub_bridge.EXPORT_DIR", self.export_dir),
            patch("weixin_hub_bridge.CONVERSATION_PATH", self.conversation_path),
            patch("weixin_hub_bridge.PENDING_TASKS_PATH", self.pending_tasks_path),
            patch("weixin_hub_bridge.PROJECT_SPACES_PATH", self.project_spaces_path),
            patch("weixin_hub_bridge.EVENT_LOG_PATH", self.event_log_path),
            patch("weixin_hub_bridge.MESSAGE_AUDIT_LOG_PATH", self.message_audit_log_path),
            patch("weixin_hub_bridge.RESTART_NOTICE_PATH", self.restart_notice_path),
            patch("weixin_hub_bridge.SERVICE_ACTION_STATE_FILE", self.service_action_state_path),
            patch("weixin_hub_bridge.STATE_PATH", self.state_path),
            patch("weixin_hub_bridge.SESSION_DIR", self.session_dir),
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

    def test_message_account_scope_rejects_stale_queued_reply(self) -> None:
        bridge = object.__new__(WeixinBridge)
        bridge.config = SimpleNamespace(active_account_id="new@im.bot")
        bridge.account_path = Path("/tmp/new@im.bot.json")

        self.assertFalse(
            WeixinBridge._message_matches_active_account(
                bridge,
                {
                    "account_id": "old@im.bot",
                    "account_file": "/tmp/old@im.bot.json",
                },
            )
        )
        self.assertTrue(
            WeixinBridge._message_matches_active_account(
                bridge,
                {
                    "account_id": "new@im.bot",
                    "account_file": "/tmp/new@im.bot.json",
                },
            )
        )
        self.assertTrue(WeixinBridge._message_matches_active_account(bridge, {}))

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
        self.assertIn("/restart [bridge|status]", reply)
        self.assertIn("/clear", reply)
        self.assertIn("\n\nNormal messages:", reply)
        self.assertNotIn("/manage", reply)

    def test_duplicate_control_message_with_different_ids_is_deduplicated(self) -> None:
        sent_texts: list[str] = []

        def capture_send(_base_url, _token, _to_user_id, _context_token, text: str) -> None:
            sent_texts.append(text)

        self.bridge._send_text = capture_send  # type: ignore[method-assign]
        message_one = {
            "message_type": 1,
            "from_user_id": "sender-test",
            "context_token": "ctx-1",
            "msg_id": "msg-a",
            "item_list": [{"type": 1, "text_item": {"text": "/help"}}],
        }
        message_two = {
            "message_type": 1,
            "from_user_id": "sender-test",
            "context_token": "ctx-1",
            "msg_id": "msg-b",
            "item_list": [{"type": 1, "text_item": {"text": "/help"}}],
        }

        self.bridge._handle_message("https://example.com", "token", message_one)
        self.bridge._handle_message("https://example.com", "token", message_two)

        self.assertEqual(1, len(sent_texts))
        self.assertIn("/restart [bridge|status]", sent_texts[0])

    def test_restart_command_schedules_full_restart(self) -> None:
        with patch("weixin_hub_bridge.schedule_named_action", return_value=SimpleNamespace(message="scheduled all")) as mocked_schedule:
            reply, handled = self.bridge._handle_control_command("sender-test", "/restart")
        self.assertTrue(handled)
        self.assertEqual("scheduled all", reply)
        mocked_schedule.assert_called_once_with("restart", delay_seconds=1.0)
        payload = json.loads(self.restart_notice_path.read_text(encoding="utf-8"))
        self.assertEqual("sender-test", payload["sender_id"])
        self.assertEqual("all", payload["scope"])

    def test_restart_bridge_command_schedules_bridge_restart(self) -> None:
        with patch("weixin_hub_bridge.schedule_named_action", return_value=SimpleNamespace(message="scheduled bridge")) as mocked_schedule:
            reply, handled = self.bridge._handle_control_command("sender-test", "/restart bridge")
        self.assertTrue(handled)
        self.assertEqual("scheduled bridge", reply)
        mocked_schedule.assert_called_once_with("restart-bridge", delay_seconds=1.0)

    def test_restart_command_rejects_unknown_scope(self) -> None:
        reply, handled = self.bridge._handle_control_command("sender-test", "/restart hub")
        self.assertTrue(handled)
        self.assertEqual("Usage: /restart, /restart bridge, or /restart status", reply)

    def test_restart_status_renders_latest_action_state(self) -> None:
        self.service_action_state_path.write_text(
            json.dumps(
                {
                    "request_id": "svc-123",
                    "action": "restart",
                    "status": "succeeded",
                    "updated_at": "2026-04-23T15:45:00",
                    "hub_pid_before": 100,
                    "bridge_pid_before": 200,
                    "hub_pid_after": 300,
                    "bridge_pid_after": 400,
                    "result_message": "Bridge stopped | Hub started | Bridge started",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        reply, handled = self.bridge._handle_control_command("sender-test", "/restart status")
        self.assertTrue(handled)
        self.assertIn("Latest restart state", reply)
        self.assertIn("Request ID: svc-123", reply)
        self.assertIn("PIDs before restart", reply)
        self.assertIn("PIDs after restart", reply)
        self.assertIn("Result: Bridge stopped | Hub started | Bridge started", reply)

    def test_deliver_pending_restart_notice_sends_direct_message_and_clears_file(self) -> None:
        self.restart_notice_path.write_text(
            json.dumps(
                {
                    "sender_id": "sender-test",
                    "context_token": "ctx-restart",
                    "scope": "all",
                    "requested_at": "2026-04-23T14:20:00",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        bridge = FeedbackBridge(BridgeConfig.load(), [])
        with patch.object(bridge, "_load_account", return_value={"token": "bot-token", "baseUrl": "https://example.com"}):
            bridge._deliver_pending_restart_notice()
        self.assertTrue(any("服务已重启成功" in text for text in bridge.sent_texts))
        self.assertFalse(self.restart_notice_path.exists())

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

    def test_new_sender_defaults_to_default_session(self) -> None:
        binding = self.bridge._ensure_conversation("sender-new")
        self.assertEqual("default", binding.current_session)
        self.assertEqual("default", binding.last_regular_session)
        self.assertIn("default", binding.sessions)

    def test_obsolete_manage_command_is_rejected_as_unknown(self) -> None:
        reply, handled = self.bridge._handle_control_command("sender-test", "/manage on")
        self.assertTrue(handled)
        self.assertEqual("Unknown command. Send /help to see supported commands.", reply)
        self.assertEqual("default", self.bridge.conversations["sender-test"].current_session)
        self.assertEqual("default", self.bridge.conversations["sender-test"].last_regular_session)

        reply, handled = self.bridge._handle_control_command("sender-test", "/manage off")
        self.assertTrue(handled)
        self.assertEqual("Unknown command. Send /help to see supported commands.", reply)
        self.assertEqual("default", self.bridge.conversations["sender-test"].current_session)

    def test_handle_message_routes_new_sender_to_default_session(self) -> None:
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
        self.assertEqual("wechat", bridge.submit_payloads[-1]["source"])
        self.assertEqual("default", bridge.submit_payloads[-1]["session_name"])
        self.assertEqual([], bridge.sent_texts)
        audits = [json.loads(line) for line in self.message_audit_log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual("task_submission", audits[-1]["route"])
        self.assertEqual("wechat", audits[-1]["source"])

    def test_passthrough_model_command_starts_dynamic_native_menu(self) -> None:
        bridge = FeedbackBridge(BridgeConfig.load(), [])
        with patch.object(
            bridge,
            "_load_codex_model_catalog",
            return_value=[
                {
                    "slug": "gpt-5.4",
                    "display_name": "gpt-5.4",
                    "description": "Latest flagship",
                    "default_reasoning": "medium",
                    "reasoning_levels": ["low", "medium", "high"],
                },
                {
                    "slug": "gpt-5.4-mini",
                    "display_name": "gpt-5.4-mini",
                    "description": "Smaller model",
                    "default_reasoning": "medium",
                    "reasoning_levels": ["medium", "high"],
                },
            ],
        ):
            bridge._handle_message(
                "https://example.com",
                "token",
                {
                    "client_id": "model-start",
                    "message_type": 1,
                    "from_user_id": "sender-test",
                    "context_token": "ctx",
                    "item_list": [{"type": 1, "text_item": {"text": "//model"}}],
                },
            )
        self.assertEqual([], bridge.submit_payloads)
        self.assertIn("Select a model", bridge.sent_texts[-1])
        self.assertIn("1. gpt-5.4 - Latest flagship", bridge.sent_texts[-1])
        self.assertEqual("/model", bridge.conversations["sender-test"].sessions["default"].native_menu_command)
        self.assertEqual("select_model", bridge.conversations["sender-test"].sessions["default"].native_menu_stage)

    def test_unsupported_passthrough_slash_command_is_rejected(self) -> None:
        bridge = FeedbackBridge(BridgeConfig.load(), [])
        bridge._handle_message(
            "https://example.com",
            "token",
            {
                "client_id": "status-pass",
                "message_type": 1,
                "from_user_id": "sender-test",
                "context_token": "ctx",
                "item_list": [{"type": 1, "text_item": {"text": "//foo"}}],
            },
        )
        self.assertEqual([], bridge.submit_payloads)
        self.assertIn("/foo", bridge.sent_texts[-1])
        self.assertIn("//model", bridge.sent_texts[-1])

    def test_passthrough_status_returns_codex_status_panel_without_submitting_task(self) -> None:
        sent_texts: list[str] = []

        def capture_send(_base_url, _token, _to_user_id, _context_token, text: str) -> None:
            sent_texts.append(text)

        self.bridge._send_text = capture_send  # type: ignore[method-assign]
        message = {
            "message_type": 1,
            "from_user_id": "sender-test",
            "context_token": "ctx-1",
            "msg_id": "msg-status",
            "item_list": [{"type": 1, "text_item": {"text": "//status"}}],
        }
        self.bridge._handle_message("https://example.com", "token", message)
        self.assertEqual([], self.bridge.submit_payloads)
        self.assertEqual(1, len(sent_texts))
        self.assertIn("OpenAI Codex", sent_texts[0])
        self.assertIn("Rate limits: unavailable", sent_texts[0])

    def test_passthrough_status_reports_codex_status_query_failure(self) -> None:
        sent_texts: list[str] = []

        def capture_send(_base_url, _token, _to_user_id, _context_token, text: str) -> None:
            sent_texts.append(text)

        self.bridge._send_text = capture_send  # type: ignore[method-assign]
        message = {
            "message_type": 1,
            "from_user_id": "sender-test",
            "context_token": "ctx-1",
            "msg_id": "msg-status-failed",
            "item_list": [{"type": 1, "text_item": {"text": "//status"}}],
        }
        self.bridge.codex_status_response = IpcResponseEnvelope(ok=False, error="codex app-server thread/resume failed")
        self.bridge._handle_message("https://example.com", "token", message)
        self.assertEqual([], self.bridge.submit_payloads)
        self.assertEqual(1, len(sent_texts))
        self.assertIn("Codex 状态查询失败", sent_texts[0])
        self.assertIn("thread/resume failed", sent_texts[0])

    def test_native_model_menu_updates_session_config_and_submit_payload(self) -> None:
        bridge = FeedbackBridge(BridgeConfig.load(), [])
        catalog = [
            {
                "slug": "gpt-5.4",
                "display_name": "gpt-5.4",
                "description": "Latest flagship",
                "default_reasoning": "medium",
                "reasoning_levels": ["low", "medium", "high"],
            },
            {
                "slug": "gpt-5.4-mini",
                "display_name": "gpt-5.4-mini",
                "description": "Smaller model",
                "default_reasoning": "medium",
                "reasoning_levels": ["medium", "high"],
            },
        ]
        with patch.object(bridge, "_load_codex_model_catalog", return_value=catalog):
            bridge._handle_message(
                "https://example.com",
                "token",
                {
                    "message_type": 1,
                    "from_user_id": "sender-test",
                    "context_token": "ctx",
                    "item_list": [{"type": 1, "text_item": {"text": "//model"}}],
                },
            )
        bridge._handle_message(
            "https://example.com",
            "token",
            {
                "client_id": "model-pick",
                "message_type": 1,
                "from_user_id": "sender-test",
                "context_token": "ctx",
                "item_list": [{"type": 1, "text_item": {"text": "2"}}],
            },
        )
        self.assertIn("Select a reasoning effort", bridge.sent_texts[-1])
        bridge._handle_message(
            "https://example.com",
            "token",
            {
                "client_id": "reasoning-pick",
                "message_type": 1,
                "from_user_id": "sender-test",
                "context_token": "ctx",
                "item_list": [{"type": 1, "text_item": {"text": "2"}}],
            },
        )
        session_meta = bridge.conversations["sender-test"].sessions["default"]
        self.assertEqual("gpt-5.4-mini", session_meta.model)
        self.assertEqual("high", session_meta.reasoning_effort)
        self.assertEqual("", session_meta.native_menu_command)
        bridge._handle_message(
            "https://example.com",
            "token",
            {
                "client_id": "task-submit",
                "message_type": 1,
                "from_user_id": "sender-test",
                "context_token": "ctx",
                "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
            },
        )
        self.assertEqual("gpt-5.4-mini", bridge.submit_payloads[-1]["model"])
        self.assertEqual("high", bridge.submit_payloads[-1]["reasoning_effort"])

    def test_native_permission_menu_updates_session_config_and_blocks_unrelated_text(self) -> None:
        bridge = FeedbackBridge(BridgeConfig.load(), [])
        bridge._handle_message(
            "https://example.com",
            "token",
            {
                "client_id": "perm-start",
                "message_type": 1,
                "from_user_id": "sender-test",
                "context_token": "ctx",
                "item_list": [{"type": 1, "text_item": {"text": "//permissions"}}],
            },
        )
        self.assertEqual([], bridge.submit_payloads)
        self.assertIn("Select a permission mode", bridge.sent_texts[-1])
        bridge._handle_message(
            "https://example.com",
            "token",
            {
                "client_id": "perm-invalid",
                "message_type": 1,
                "from_user_id": "sender-test",
                "context_token": "ctx",
                "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
            },
        )
        self.assertEqual([], bridge.submit_payloads)
        self.assertIn("Invalid selection", bridge.sent_texts[-1])
        bridge._handle_message(
            "https://example.com",
            "token",
            {
                "client_id": "perm-pick",
                "message_type": 1,
                "from_user_id": "sender-test",
                "context_token": "ctx",
                "item_list": [{"type": 1, "text_item": {"text": "1"}}],
            },
        )
        session_meta = bridge.conversations["sender-test"].sessions["default"]
        self.assertEqual("default", session_meta.permission_mode)
        self.assertEqual("", session_meta.native_menu_command)
        bridge._handle_message(
            "https://example.com",
            "token",
            {
                "client_id": "perm-submit",
                "message_type": 1,
                "from_user_id": "sender-test",
                "context_token": "ctx",
                "item_list": [{"type": 1, "text_item": {"text": "run a task"}}],
            },
        )
        self.assertEqual("default", bridge.submit_payloads[-1]["permission_mode"])

    def test_bridge_defaults_session_feedback_to_chinese_when_auto_and_env_unset(self) -> None:
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

    def test_notify_service_started_broadcasts_service_notice(self) -> None:
        bridge = FeedbackBridge(BridgeConfig.load(), [])
        with patch("weixin_hub_bridge.broadcast_weixin_notice_by_kind") as mocked_broadcast:
            mocked_broadcast.return_value = SimpleNamespace(summary="已通知 1 个微信会话", error="")
            bridge._notify_service_started()
        mocked_broadcast.assert_called_once()
        self.assertEqual("service", mocked_broadcast.call_args.args[0])
        self.assertEqual("Bridge 启动", mocked_broadcast.call_args.args[1])
        self.assertIn("默认 Agent", mocked_broadcast.call_args.args[2])

    def test_notify_service_started_logs_summary(self) -> None:
        bridge = FeedbackBridge(BridgeConfig.load(), [])
        with patch("weixin_hub_bridge.broadcast_weixin_notice_by_kind") as mocked_broadcast:
            mocked_broadcast.return_value = SimpleNamespace(summary="已通知 1 个微信会话", error="")
            with patch("builtins.print") as mocked_print:
                bridge._notify_service_started()
        mocked_print.assert_any_call("[bridge] startup notice: 已通知 1 个微信会话", flush=True)

    def test_notify_service_started_skips_broadcast_when_restart_notice_pending(self) -> None:
        bridge = FeedbackBridge(BridgeConfig.load(), [])
        self.restart_notice_path.write_text(
            json.dumps(
                {
                    "sender_id": "sender-test",
                    "context_token": "ctx",
                    "scope": "all",
                    "requested_at": "2026-04-24T04:55:00",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with (
            patch("weixin_hub_bridge.broadcast_weixin_notice_by_kind") as mocked_broadcast,
            patch("builtins.print") as mocked_print,
        ):
            bridge._notify_service_started()
        mocked_broadcast.assert_not_called()
        mocked_print.assert_any_call(
            "[bridge] startup notice skipped: pending restart notice will be delivered directly",
            flush=True,
        )

    def test_notify_test_command_returns_delivery_summary(self) -> None:
        with patch("weixin_hub_bridge.broadcast_weixin_notice_by_kind") as mocked_broadcast:
            mocked_broadcast.return_value = SimpleNamespace(summary="已通知 1/4 个微信会话，剩余发送失败：missing context token", error="missing context token")
            reply, handled = self.bridge._handle_control_command("sender-test", "/notify test")
        self.assertTrue(handled)
        self.assertIn("Delivery result: 已通知 1/4 个微信会话", reply)

    def test_handle_message_routes_session_request_inside_current_session(self) -> None:
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
        self.assertEqual("wechat", bridge.submit_payloads[-1]["source"])
        self.assertEqual("default", bridge.submit_payloads[-1]["session_name"])

    def test_handle_message_routes_session_style_prompt_without_switching_session(self) -> None:
        bridge = FeedbackBridge(BridgeConfig.load(), [])
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
        self.assertEqual("wechat", bridge.submit_payloads[-1]["source"])
        self.assertEqual("default", bridge.submit_payloads[-1]["session_name"])
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
                "item_list": [{"type": 1, "text_item": {"text": "/obsolete"}}],
            },
        )
        self.assertEqual([], bridge.submit_payloads)
        audits = [json.loads(line) for line in self.message_audit_log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual("control_command", audits[-1]["route"])
        self.assertEqual("/obsolete", audits[-1]["command"])

    def test_session_style_task_result_uses_standard_task_reply(self) -> None:
        bridge = FeedbackBridge(
            BridgeConfig.load(),
            [
                {
                    "id": "task-feedback-001",
                    "sender_id": "sender-manager",
                    "session_name": "default",
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
        bridge.process_next_pushed_update()
        self.assertTrue(bridge.sent_texts[-1].startswith("done · "))
        self.assertIn("\n\n找到 2 个会话：default, deep-dive", bridge.sent_texts[-1])

    def test_supported_session_prompt_still_submits_from_current_session(self) -> None:
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
        self.assertEqual("wechat", bridge.submit_payloads[-1]["source"])
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
                bridge.process_next_pushed_update()
                bridge.process_next_pushed_update()
            self.assertEqual(1, len(bridge.sent_texts))
            self.assertTrue(bridge.sent_texts[0].startswith("done · "))
            self.assertIn("\n\nworld", bridge.sent_texts[0])
            entries = [json.loads(line) for line in event_log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(["accepted", "running", "succeeded"], [entry["event"] for entry in entries])
            self.assertEqual("task-feedback-001", entries[-1]["task_id"])
            self.assertEqual("default", entries[-1]["session_name"])

    def test_handle_message_sends_progress_and_final_when_content_differs(self) -> None:
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
                        "context_left_percent": 21,
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
                        "context_left_percent": 20,
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
                        "context_left_percent": 19,
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
                bridge.process_next_pushed_update()
                bridge.process_next_pushed_update()
                bridge.process_next_pushed_update()
            self.assertTrue(bridge.sent_texts[0].startswith("running · "))
            self.assertIn(" · ctx 21% · ", bridge.sent_texts[0].splitlines()[0])
            self.assertIn("\n\n正在分析仓库结构", bridge.sent_texts[0])
            self.assertTrue(bridge.sent_texts[1].startswith("running · "))
            self.assertIn(" · ctx 20% · ", bridge.sent_texts[1].splitlines()[0])
            self.assertIn("\n\n正在生成修复方案", bridge.sent_texts[1])
            self.assertTrue(bridge.sent_texts[2].startswith("done · "))
            self.assertIn(" · ctx 19% · ", bridge.sent_texts[2].splitlines()[0])
            self.assertIn("\n\nworld", bridge.sent_texts[2])
            entries = [json.loads(line) for line in event_log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(
                ["accepted", "running", "progress", "progress", "succeeded"],
                [entry["event"] for entry in entries],
            )
            self.assertEqual("正在生成修复方案", entries[-2]["result_preview"])

    def test_handle_message_queries_live_context_for_codex_header(self) -> None:
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
                        "context_left_percent": 18,
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
                        "context_left_percent": 18,
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
                bridge.process_next_pushed_update()
                bridge.process_next_pushed_update()
            self.assertIn(" · ctx 18% · ", bridge.sent_texts[0].splitlines()[0])
            self.assertIn(" · ctx 18% · ", bridge.sent_texts[1].splitlines()[0])

    def test_handle_message_uses_live_context_when_task_payload_has_no_cached_value(self) -> None:
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
                ],
            )
            bridge.task_context_left_response = IpcResponseEnvelope(ok=True, payload={"context_left_percent": 17})
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
                bridge.process_next_pushed_update()
            self.assertIn(" · ctx 17% · ", bridge.sent_texts[0].splitlines()[0])

    def test_pushed_task_update_sends_progress_without_polling_task_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            event_log_path = Path(temp_dir) / "weixin_bridge_events.jsonl"
            bridge = FeedbackBridge(BridgeConfig.load(), [])
            bridge.task_context_left_response = IpcResponseEnvelope(ok=True, payload={"context_left_percent": 33})
            with patch("weixin_hub_bridge.EVENT_LOG_PATH", event_log_path):
                bridge._handle_pushed_task_update(
                    "https://example.com",
                    "token",
                    {
                        "event": "progress",
                        "task": {
                            "id": "task-pushed-001",
                            "sender_id": "sender-test",
                            "session_name": "default",
                            "status": "running",
                            "agent_id": "main",
                            "agent_name": "default",
                            "backend": "codex",
                            "source": "wechat",
                            "prompt": "hello",
                            "context_token": "ctx",
                            "progress_text": "正在处理推送进度",
                            "progress_seq": 1,
                            "created_at": "2026-04-20T12:00:00",
                            "started_at": "2026-04-20T12:00:01",
                        },
                    },
                )
            self.assertEqual(1, len(bridge.sent_texts))
            self.assertIn(" · ctx 33% · ", bridge.sent_texts[0].splitlines()[0])
            self.assertIn("正在处理推送进度", bridge.sent_texts[0])
            self.assertNotIn("task-pushed-001", bridge.pending_tasks)

    def test_handle_message_starts_typing_indicator_when_task_is_accepted(self) -> None:
        bridge = TypingBridge(BridgeConfig.load(), [])
        with patch("weixin_hub_bridge.time.time", return_value=100):
            with patch.object(bridge, "_ensure_typing_worker_started"):
                bridge._handle_message(
                    "https://example.com",
                    "token",
                    {
                        "message_type": 1,
                        "from_user_id": "sender-test",
                        "context_token": "ctx-live",
                        "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
                    },
                )
            bridge._run_typing_scheduler_once("https://example.com", "token")

        self.assertEqual(
            [
                ("getconfig", {"ilink_user_id": "sender-test", "context_token": "ctx-live", "base_info": {"channel_version": "2.1.1"}}),
                ("sendtyping", {"ilink_user_id": "sender-test", "typing_ticket": "ticket-typing", "status": 1, "base_info": {"channel_version": "2.1.1"}}),
            ],
            bridge.typing_calls,
        )

    def test_pushed_terminal_update_stops_typing_indicator(self) -> None:
        bridge = TypingBridge(
            BridgeConfig.load(),
            [
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
                    "finished_at": "2026-04-20T12:00:03",
                }
            ],
        )
        tracked = WeixinPendingTaskState(
            task_id="task-feedback-001",
            sender_id="sender-test",
            session_name="default",
            backend="codex",
            context_token="ctx-live",
        )
        bridge.pending_tasks["task-feedback-001"] = tracked
        tracked.typing_ticket = "ticket-typing"
        tracked.typing_last_sent_at = 95

        class ImmediateThread:
            def __init__(self, target, args=(), daemon=None) -> None:
                self.target = target
                self.args = args
                self.daemon = daemon

            def start(self) -> None:
                self.target(*self.args)

        with patch("weixin_hub_bridge.threading.Thread", ImmediateThread):
            bridge.process_next_pushed_update()

        self.assertEqual(
            [
                ("sendtyping", {"ilink_user_id": "sender-test", "typing_ticket": "ticket-typing", "status": 2, "base_info": {"channel_version": "2.1.1"}}),
            ],
            bridge.typing_calls,
        )

    def test_handle_message_sends_completion_notice_for_duplicate_final_result_after_progress(self) -> None:
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
                        "progress_text": "最终回答",
                        "progress_seq": 1,
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
                        "output": "最终回答",
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
                bridge.process_next_pushed_update()
                bridge.process_next_pushed_update()
            self.assertEqual(2, len(bridge.sent_texts))
            self.assertTrue(bridge.sent_texts[0].startswith("running · "))
            self.assertIn("\n\n最终回答", bridge.sent_texts[0])
            self.assertTrue(bridge.sent_texts[1].startswith("done · "))
            self.assertNotIn("\n\n", bridge.sent_texts[1])
            self.assertNotIn("\n\n最终回答", bridge.sent_texts[1])
            entries = [json.loads(line) for line in event_log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(
                ["accepted", "running", "progress", "succeeded"],
                [entry["event"] for entry in entries],
            )

    def test_session_style_task_sends_incremental_progress_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            event_log_path = Path(temp_dir) / "weixin_bridge_events.jsonl"
            bridge = FeedbackBridge(
                BridgeConfig.load(),
                [
                    {
                        "id": "task-feedback-001",
                        "sender_id": "sender-manager",
                        "session_name": "default",
                        "status": "running",
                        "agent_id": "main",
                        "agent_name": "default",
                        "backend": "codex",
                        "prompt": "列出所有会话",
                        "progress_text": "正在调用 get_sender_snapshot",
                        "progress_seq": 1,
                        "created_at": "2026-04-20T12:00:00",
                    },
                    {
                        "id": "task-feedback-001",
                        "sender_id": "sender-manager",
                        "session_name": "default",
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
                bridge.process_next_pushed_update()
                bridge.process_next_pushed_update()
            self.assertEqual(2, len(bridge.sent_texts))
            self.assertTrue(bridge.sent_texts[0].startswith("running · "))
            self.assertIn("\n\n正在调用 get_sender_snapshot", bridge.sent_texts[0])
            self.assertTrue(bridge.sent_texts[1].startswith("done · "))
            self.assertIn("\n\n找到 2 个会话：default, deep-dive", bridge.sent_texts[1])
            entries = [json.loads(line) for line in event_log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(
                ["accepted", "running", "progress", "succeeded"],
                [entry["event"] for entry in entries],
            )
            self.assertEqual("正在调用 get_sender_snapshot", entries[-2]["result_preview"])

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
                bridge.process_next_pushed_update()
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
        bridge.process_next_pushed_update()
        bridge.process_next_pushed_update()
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
        bridge.process_next_pushed_update()
        self.assertTrue(bridge.sent_texts[-1].startswith("done · "))
        self.assertIn("\n\nworld", bridge.sent_texts[-1])

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
                        "result_preview": "正在调用 get_sender_snapshot",
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
        self.assertIn("正在调用 get_sender_snapshot", reply)

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
                                "session_name": "default",
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
        project_dir = self.app_dir / "workspace" / "project-gamma"
        project_dir.mkdir(parents=True, exist_ok=True)
        reply, handled = self.bridge._handle_control_command("sender-test", "/project project-gamma")
        self.assertTrue(handled)
        self.assertIn(str(project_dir.resolve()), reply)
        binding = self.bridge.conversations["sender-test"]
        self.assertEqual(str(project_dir.resolve()), binding.sessions["default"].workdir)

    def test_new_session_inherits_current_project_directory(self) -> None:
        project_dir = self.app_dir / "workspace" / "project-theta"
        project_dir.mkdir(parents=True, exist_ok=True)
        self.bridge._handle_control_command("sender-test", "/project project-theta")
        reply, handled = self.bridge._handle_control_command("sender-test", "/new feature-a")
        self.assertTrue(handled)
        self.assertIn("feature-a", reply)
        binding = self.bridge.conversations["sender-test"]
        self.assertEqual(str(project_dir.resolve()), binding.sessions["feature-a"].workdir)

    def test_project_reset_clears_session_override(self) -> None:
        project_dir = self.app_dir / "workspace" / "project-epsilon"
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
        project_dir = self.app_dir / "workspace" / "project-delta"
        project_dir.mkdir(parents=True, exist_ok=True)
        fake_hub_config = SimpleNamespace(
            agents=[_fake_agent("main", backend="codex", model="", workdir="/tmp/project-alpha", name="Main")]
        )
        with patch("weixin_hub_bridge.HubConfig.load", return_value=fake_hub_config):
            reply, handled = self.bridge._handle_control_command("sender-test", "/project list")
        self.assertTrue(handled)
        self.assertIn("Available project directories:", reply)
        self.assertIn("project-delta", reply)

    def test_project_add_registers_external_directory(self) -> None:
        project_dir = Path(self._tempdir.name) / "external-project"
        project_dir.mkdir(parents=True, exist_ok=True)
        reply, handled = self.bridge._handle_control_command("sender-test", f"/project add external {project_dir}")
        self.assertTrue(handled)
        self.assertIn("Registered project", reply)
        self.assertIn(str(project_dir.resolve()), reply)
        saved = json.loads(self.project_spaces_path.read_text(encoding="utf-8"))
        self.assertEqual(str(project_dir.resolve()), saved["projects"]["external"])

    def test_project_remove_deletes_registered_directory(self) -> None:
        project_dir = Path(self._tempdir.name) / "external-project"
        project_dir.mkdir(parents=True, exist_ok=True)
        self.project_spaces_path.write_text(
            json.dumps({"projects": {"external": str(project_dir.resolve())}}, ensure_ascii=False),
            encoding="utf-8",
        )
        reply, handled = self.bridge._handle_control_command("sender-test", "/project remove external")
        self.assertTrue(handled)
        self.assertIn("Removed project: external", reply)
        saved = json.loads(self.project_spaces_path.read_text(encoding="utf-8"))
        self.assertEqual({}, saved["projects"])

    def test_list_command_includes_session_summary(self) -> None:
        reply, handled = self.bridge._handle_control_command("sender-test", "/list")
        self.assertTrue(handled)
        self.assertIn("Sessions:", reply)
        self.assertIn("current project:", reply)
        self.assertIn("page 1/1", reply)
        self.assertIn("deep-dive [claude]", reply)
        self.assertIn("stacktrace details", reply)
        lines = reply.splitlines()
        self.assertIn("deep-dive [claude]", lines[2])
        self.assertIn("default [codex]", lines[3])
        self.assertIn("zzz-empty [opencode]", lines[4])

    def test_sessions_all_lists_every_project_scope(self) -> None:
        self.bridge.conversations["sender-test"].sessions["api-project"] = self.bridge._new_session_meta(
            "codex",
            workdir=str((Path(self._tempdir.name) / "api-project").resolve()),
        )
        Path(self._tempdir.name, "api-project").mkdir(parents=True, exist_ok=True)
        reply, handled = self.bridge._handle_control_command("sender-test", "/sessions all")
        self.assertTrue(handled)
        self.assertIn("all projects", reply)
        self.assertIn("api-project [codex]", reply)

    def test_project_sessions_lists_only_target_project_sessions(self) -> None:
        shared_dir = Path(self._tempdir.name) / "project-omega"
        other_dir = Path(self._tempdir.name) / "project-sigma"
        shared_dir.mkdir(parents=True, exist_ok=True)
        other_dir.mkdir(parents=True, exist_ok=True)
        self.project_spaces_path.write_text(
            json.dumps(
                {
                    "projects": {
                        "omega": str(shared_dir.resolve()),
                        "sigma": str(other_dir.resolve()),
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        binding = self.bridge.conversations["sender-test"]
        binding.sessions["deep-dive"].workdir = str(shared_dir.resolve())
        binding.sessions["default"].workdir = str(other_dir.resolve())
        reply, handled = self.bridge._handle_control_command("sender-test", "/project sessions omega")
        self.assertTrue(handled)
        self.assertIn("current project: omega", reply)
        self.assertIn("deep-dive [claude]", reply)
        self.assertNotIn("default [codex]", reply)

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

    def test_clear_command_clears_current_agent_session_id(self) -> None:
        session_file = self.session_dir / "main__default.txt"
        session_file.write_text("codex-thread-id", encoding="utf-8")

        reply, handled = self.bridge._handle_control_command("sender-test", "/clear")

        self.assertTrue(handled)
        self.assertIn("Cleared current agent session", reply)
        self.assertEqual("", session_file.read_text(encoding="utf-8"))
        binding = self.bridge.conversations["sender-test"]
        self.assertIn("default", binding.sessions)
        self.assertEqual("default", binding.current_session)

    def test_clear_command_cancels_active_current_session_task(self) -> None:
        session_file = self.session_dir / "main__default.txt"
        session_file.write_text("codex-thread-id", encoding="utf-8")
        self.bridge._state_payload.payload["tasks"].append(
            {
                "id": "task-running-clear",
                "sender_id": "sender-test",
                "session_name": "default",
                "status": "running",
                "agent_id": "main",
                "agent_name": "default",
                "backend": "codex",
                "prompt": "stuck",
                "created_at": "2026-04-20T12:10:00",
                "started_at": "2026-04-20T12:10:00",
            }
        )
        self.bridge.pending_tasks["task-running-clear"] = WeixinPendingTaskState(
            task_id="task-running-clear",
            sender_id="sender-test",
            session_name="default",
            backend="codex",
        )

        reply, handled = self.bridge._handle_control_command("sender-test", "/clear")

        self.assertTrue(handled)
        self.assertIn("Cleared current agent session", reply)
        self.assertIn("Also canceled active tasks", reply)
        self.assertEqual("", session_file.read_text(encoding="utf-8"))
        self.assertNotIn("task-running-clear", self.bridge.pending_tasks)
        matching = next(task for task in self.bridge._state_payload.payload["tasks"] if task["id"] == "task-running-clear")
        self.assertEqual("canceled", matching["status"])

    def test_clear_command_reports_already_clear_session(self) -> None:
        reply, handled = self.bridge._handle_control_command("sender-test", "/clear")

        self.assertTrue(handled)
        self.assertIn("already clear", reply)

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
        export_path = self.export_dir / "sender-test__deep-dive.md"
        self.assertTrue(export_path.exists())
        content = export_path.read_text(encoding="utf-8")
        self.assertIn("# Session Export: deep-dive", content)
        self.assertIn("investigate bug", content)

    def test_showfile_command_previews_project_text_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            docs_dir = project_root / "docs"
            docs_dir.mkdir()
            (docs_dir / "architecture.md").write_text("# Architecture\n\nBridge -> Hub\n", encoding="utf-8")
            with patch("weixin_hub_bridge.APP_DIR", project_root):
                reply, handled = self.bridge._handle_control_command("sender-test", "/showfile docs/architecture.md")
        self.assertTrue(handled)
        self.assertIn("File preview", reply)
        self.assertIn(f"Path: {Path('docs') / 'architecture.md'}", reply)
        self.assertIn("Bridge -> Hub", reply)

    def test_showfile_command_denies_sensitive_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            accounts_dir = project_root / "accounts"
            accounts_dir.mkdir()
            (accounts_dir / "wechat-bot.json").write_text('{"token":"secret"}', encoding="utf-8")
            with patch("weixin_hub_bridge.APP_DIR", project_root):
                reply, handled = self.bridge._handle_control_command("sender-test", "/showfile accounts/wechat-bot.json")
        self.assertTrue(handled)
        self.assertIn("File preview denied", reply)

    def test_sendfile_command_sends_project_file(self) -> None:
        bridge = FeedbackBridge(BridgeConfig.load(), [])
        sent_paths: list[Path] = []

        def capture_send_media(_base_url, _token, _to_user_id, _context_token, file_path: Path) -> None:
            sent_paths.append(file_path)

        bridge._send_media_file = capture_send_media  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            docs_dir = project_root / "docs"
            docs_dir.mkdir()
            media_path = docs_dir / "architecture.md"
            media_path.write_text("# Architecture\n", encoding="utf-8")
            with patch("weixin_hub_bridge.APP_DIR", project_root):
                bridge._handle_message(
                    "https://example.com",
                    "token",
                    {
                        "message_type": 1,
                        "from_user_id": "sender-test",
                        "context_token": "ctx",
                        "item_list": [{"type": 1, "text_item": {"text": "/sendfile docs/architecture.md"}}],
                    },
                )
        self.assertEqual(1, len(sent_paths))
        self.assertEqual("architecture.md", sent_paths[0].name)
        self.assertEqual([], bridge.sent_texts)

    def test_send_media_file_builds_image_message_item(self) -> None:
        bridge = FakeBridge(BridgeConfig.load())
        sent_bodies: list[dict[str, object]] = []

        def fake_upload(_base_url, _token, _to_user_id, _file_path, *, media_type: int) -> dict[str, object]:
            self.assertEqual(1, media_type)
            return {
                "download_param": "download-param",
                "aes_hex": "00112233445566778899aabbccddeeff",
                "raw_size": 3,
                "cipher_size": 16,
                "md5": "md5",
            }

        def fake_post(_url, body, *, token, timeout_ms):
            sent_bodies.append(body)
            return {"ret": 0}

        bridge._upload_media_file = fake_upload  # type: ignore[method-assign]
        bridge._post_json = fake_post  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "diagram.png"
            image_path.write_bytes(b"png")
            bridge._send_media_file("https://example.com", "token", "sender-test", "ctx", image_path)
        item = sent_bodies[0]["msg"]["item_list"][0]  # type: ignore[index]
        self.assertEqual(2, item["type"])
        self.assertEqual("download-param", item["image_item"]["media"]["encrypt_query_param"])
        self.assertEqual("MDAxMTIyMzM0NDU1NjY3Nzg4OTlhYWJiY2NkZGVlZmY=", item["image_item"]["media"]["aes_key"])
        self.assertEqual(16, item["image_item"]["mid_size"])

    def test_send_media_file_rejects_sendmessage_error_code(self) -> None:
        bridge = FakeBridge(BridgeConfig.load())

        def fake_upload(_base_url, _token, _to_user_id, _file_path, *, media_type: int) -> dict[str, object]:
            return {
                "download_param": "download-param",
                "aes_hex": "00112233445566778899aabbccddeeff",
                "raw_size": 3,
                "cipher_size": 16,
                "md5": "md5",
            }

        def fake_post(_url, _body, *, token, timeout_ms):
            return {"ret": -2}

        bridge._upload_media_file = fake_upload  # type: ignore[method-assign]
        bridge._post_json = fake_post  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "diagram.png"
            image_path.write_bytes(b"png")
            with self.assertRaisesRegex(RuntimeError, "sendmessage returned ret=-2"):
                bridge._send_media_file("https://example.com", "token", "sender-test", "ctx", image_path)

    def test_deliver_text_now_rejects_sendmessage_error_code(self) -> None:
        bridge = FakeBridge(BridgeConfig.load())

        def fake_post(_url, _body, *, token, timeout_ms):
            return {"ret": -2}

        bridge._post_json = fake_post  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "sendmessage returned ret=-2"):
            bridge._deliver_text_now("https://example.com", "token", "sender-test", "ctx", "hello")

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
        self.assertIn("Current project: project-alpha", reply)
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

