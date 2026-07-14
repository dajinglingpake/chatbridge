from __future__ import annotations

import tempfile
import threading
import time
import unittest
import socket
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.bridge_pending_tasks import BridgePendingReplyTask
from core.bridge_runtime import BridgeSubmittedTask, IncomingBridgeMessage
from core.state_models import HubTask
import local_ipc
from qq_onebot_bridge import DEFAULT_QQ_AGENT_ID, QQ_GROUP_ATTACHMENT_DIR_NAME, WORKSPACE_DIR, QQOneBotBridge
from http.server import ThreadingHTTPServer


NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class FakeQQBridge(QQOneBotBridge):
    def __init__(self, temp_path: Path) -> None:
        self.temp_path = temp_path
        self.submitted: list[tuple[str, str]] = []
        self.replies: list[tuple[dict[str, object], str]] = []
        self.api_calls: list[tuple[str, dict[str, object]]] = []
        self.ipc_responses: dict[str, object] = {}
        super().__init__(
            SimpleNamespace(
                backend_id="main",
                default_backend="codex",
                hub_task_timeout_seconds=30,
                service_notice_enabled=True,
                config_notice_enabled=True,
                task_notice_enabled=True,
                save=lambda: None,
            ),
            api_base="http://onebot.local",
        )

    def _download_media_url(self, sender_key: str, url: str, *, filename: str) -> Path:
        target = self.temp_path / f"{sender_key.replace(':', '-')}-{filename}"
        target.write_bytes(f"{url}:{filename}".encode("utf-8"))
        return target

    def _submit_task(self, sender_key: str, prompt: str) -> dict[str, object]:
        self.submitted.append((sender_key, prompt))
        return {"id": "task-qq-001"}

    def _submit_runtime_task(
        self,
        message: IncomingBridgeMessage,
        _session: object,
        prompt: str,
        _passthrough: bool,
    ) -> BridgeSubmittedTask:
        self.submitted.append((message.sender_id, prompt))
        payload = {"id": "task-qq-001"}
        return BridgeSubmittedTask(task_id="task-qq-001", payload=payload)

    def _wait_and_reply(self, event: dict[str, object], task_id: str) -> None:
        self.replies.append((event, task_id))

    def _onebot_api(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        self.api_calls.append((action, payload))
        return {"status": "ok"}

    def _ipc_request(self, action: str, payload: dict[str, object], *, timeout_seconds: float) -> object:
        self.api_calls.append((f"ipc:{action}", payload))
        return self.ipc_responses.get(action, SimpleNamespace(ok=True, error="", payload={}))


class QQOneBotBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.temp_path = Path(self._tempdir.name)
        self.state_path = self.temp_path / "qq_media.json"
        self.pending_tasks_path = self.temp_path / "qq_pending_tasks.json"
        self.conversations_path = self.temp_path / "qq_conversations.json"
        self.service_action_state_path = self.temp_path / "service_action_state.json"
        media_patcher = patch("qq_onebot_bridge.ONEBOT_STATE_PATH", self.state_path)
        task_patcher = patch("qq_onebot_bridge.QQ_PENDING_TASKS_PATH", self.pending_tasks_path)
        conversations_patcher = patch("qq_onebot_bridge.QQ_CONVERSATIONS_PATH", self.conversations_path)
        service_action_patcher = patch("qq_onebot_bridge.SERVICE_ACTION_STATE_PATH", self.service_action_state_path)
        media_patcher.start()
        task_patcher.start()
        conversations_patcher.start()
        service_action_patcher.start()
        self.addCleanup(media_patcher.stop)
        self.addCleanup(task_patcher.stop)
        self.addCleanup(conversations_patcher.stop)
        self.addCleanup(service_action_patcher.stop)

    def test_media_only_message_caches_attachment_for_next_text(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message": [
                    {"type": "image", "data": {"url": "http://media.local/image", "file": "image.png"}},
                ],
            }
        )

        self.assertEqual([], bridge.submitted)
        self.assertIn("qq:private:10001", bridge.pending_media_context)
        self.assertEqual("send_private_msg", bridge.api_calls[-1][0])
        self.assertIn("已收到 1 个附件", bridge.api_calls[-1][1]["message"])

        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message": [{"type": "text", "data": {"text": "看下这张图"}}],
            }
        )

        self.assertEqual(1, len(bridge.submitted))
        sender_key, prompt = bridge.submitted[0]
        self.assertEqual("qq:private:10001", sender_key)
        self.assertTrue(prompt.startswith("看下这张图"))
        self.assertIn("qq-private-10001-image.png", prompt)
        self.assertNotIn("\n", prompt)
        self.assertNotIn("图片:", prompt)
        self.assertNotIn("本地路径", prompt)
        self.assertNotIn("qq:private:10001", bridge.pending_media_context)

    def test_private_text_with_image_submits_prompt_with_attachment(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message": [
                    {"type": "text", "data": {"text": "看下这张图"}},
                    {"type": "image", "data": {"url": "http://media.local/image", "file": "image.png"}},
                ],
            }
        )

        self.assertNotIn("qq:private:10001", bridge.pending_media_context)
        self.assertEqual(1, len(bridge.submitted))
        sender_key, prompt = bridge.submitted[0]
        self.assertEqual("qq:private:10001", sender_key)
        self.assertTrue(prompt.startswith("看下这张图"))
        self.assertIn("qq-private-10001-image.png", prompt)
        self.assertNotIn("\n", prompt)
        self.assertNotIn("图片:", prompt)
        self.assertNotIn("本地路径", prompt)

    def test_group_text_with_file_submits_prompt_with_attachment(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 20002,
                "user_id": 10001,
                "self_id": 900000001,
                "message": [
                    {"type": "at", "data": {"qq": "900000001"}},
                    {"type": "text", "data": {"text": "总结这个文件"}},
                    {"type": "file", "data": {"url": "http://media.local/file", "file": "report.pdf"}},
                ],
            }
        )

        sender_key, prompt = bridge.submitted[0]
        self.assertEqual("qq:group:20002", sender_key)
        self.assertTrue(prompt.startswith("总结这个文件"))
        self.assertIn("qq-group-20002-report.pdf", prompt)
        self.assertNotIn("\n", prompt)
        self.assertNotIn("文件:", prompt)
        self.assertNotIn("本地路径", prompt)

    def test_group_media_is_copied_under_group_agent_workdir(self) -> None:
        source_file = self.temp_path / "onebot-cache-report.txt"
        source_file.write_text("report-data", encoding="utf-8")
        group_workdir = self.temp_path / "qq-group-workspace"
        bridge = FakeQQBridge(self.temp_path)

        with patch.object(
            QQOneBotBridge,
            "_load_agents",
            return_value=[SimpleNamespace(id="qq-group", workdir=str(group_workdir))],
        ):
            bridge.handle_event(
                {
                    "post_type": "message",
                    "message_type": "group",
                    "group_id": 20002,
                    "user_id": 10001,
                    "self_id": 900000001,
                    "message": [
                        {"type": "at", "data": {"qq": "900000001"}},
                        {"type": "text", "data": {"text": "总结这个文件"}},
                        {"type": "file", "data": {"url": str(source_file), "file": "report.txt"}},
                    ],
                }
            )

        sender_key, prompt = bridge.submitted[0]
        self.assertEqual("qq:group:20002", sender_key)
        saved_path_text = prompt.rsplit(" ", 1)[1]
        self.assertFalse(Path(saved_path_text).is_absolute())
        self.assertIn(QQ_GROUP_ATTACHMENT_DIR_NAME, saved_path_text)
        self.assertIn("qq-group-20002", saved_path_text)
        saved_path = group_workdir / saved_path_text
        self.assertTrue(saved_path.exists())
        self.assertIn("report-data", saved_path.read_text(encoding="utf-8"))
        self.assertNotIn("I:", saved_path_text)
        self.assertNotIn("\n", prompt)

    def test_file_id_media_message_uses_onebot_get_file(self) -> None:
        source_file = self.temp_path / "onebot-cache-report.pdf"
        source_file.write_bytes(b"report-data")
        bridge = FakeQQBridge(self.temp_path)
        bridge.ipc_responses = {}

        def onebot_api(action: str, payload: dict[str, object]) -> dict[str, object]:
            bridge.api_calls.append((action, payload))
            if action == "get_file":
                return {"status": "ok", "data": {"file": str(source_file)}}
            return {"status": "ok"}

        bridge._onebot_api = onebot_api  # type: ignore[method-assign]
        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message": [
                    {"type": "file", "data": {"file_id": "file-001", "file": "report.pdf"}},
                    {"type": "text", "data": {"text": "总结这个文件"}},
                ],
            }
        )

        self.assertEqual("get_file", bridge.api_calls[0][0])
        sender_key, prompt = bridge.submitted[0]
        self.assertEqual("qq:private:10001", sender_key)
        saved_path = Path(prompt.rsplit(" ", 1)[1])
        self.assertTrue(saved_path.name.endswith("-report.pdf"))
        self.assertNotIn("文件:", prompt)
        self.assertNotIn("本地路径", prompt)
        self.assertIn("report-data", saved_path.read_bytes().decode("utf-8"))

    def test_local_path_url_media_message_copies_file_without_urlopen(self) -> None:
        source_file = self.temp_path / "onebot-cache-report.txt"
        source_file.write_text("report-data", encoding="utf-8")
        bridge = FakeQQBridge(self.temp_path)
        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message": [
                    {"type": "file", "data": {"url": str(source_file), "file": "report.txt"}},
                    {"type": "text", "data": {"text": "总结这个文件"}},
                ],
            }
        )

        sender_key, prompt = bridge.submitted[0]
        self.assertEqual("qq:private:10001", sender_key)
        saved_path = Path(prompt.rsplit(" ", 1)[1])
        self.assertTrue(saved_path.name.endswith("-report.txt"))
        self.assertNotIn("文件:", prompt)
        self.assertNotIn("本地路径", prompt)
        self.assertIn("report-data", saved_path.read_text(encoding="utf-8"))

    def test_inaccessible_local_path_url_uses_file_id_without_urlopen(self) -> None:
        source_file = self.temp_path / "onebot-cache-report.txt"
        source_file.write_text("report-data", encoding="utf-8")
        bridge = FakeQQBridge(self.temp_path)

        def onebot_api(action: str, payload: dict[str, object]) -> dict[str, object]:
            bridge.api_calls.append((action, payload))
            if action == "get_file":
                return {"status": "ok", "data": {"file": str(source_file)}}
            return {"status": "ok"}

        bridge._onebot_api = onebot_api  # type: ignore[method-assign]
        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message": [
                    {"type": "file", "data": {"url": "F:\\QQCache\\missing.txt", "file_id": "file-001", "file": "report.txt"}},
                    {"type": "text", "data": {"text": "总结这个文件"}},
                ],
            }
        )

        self.assertEqual("get_file", bridge.api_calls[0][0])
        sender_key, prompt = bridge.submitted[0]
        self.assertEqual("qq:private:10001", sender_key)
        saved_path = Path(prompt.rsplit(" ", 1)[1])
        self.assertTrue(saved_path.name.endswith("-report.txt"))
        self.assertNotIn("文件:", prompt)
        self.assertNotIn("本地路径", prompt)
        self.assertIn("report-data", saved_path.read_text(encoding="utf-8"))

    def test_group_message_without_at_self_is_ignored(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 20002,
                "user_id": 10001,
                "self_id": 900000001,
                "message": [
                    {"type": "text", "data": {"text": "不要回复这条"}},
                ],
            }
        )

        self.assertEqual([], bridge.submitted)

    def test_group_message_without_at_self_is_accepted_when_mention_not_required(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.config.qq_group_require_mention = False
        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 20002,
                "user_id": 10001,
                "self_id": 900000001,
                "message": [
                    {"type": "text", "data": {"text": "不需要 at 也处理"}},
                ],
            }
        )

        self.assertEqual([("qq:group:20002", "不需要 at 也处理")], bridge.submitted)

    def test_group_text_name_mention_with_napcat_at_type_is_accepted(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge._login_nickname = "测试机器人"
        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 20002,
                "user_id": 10001,
                "self_id": 900000001,
                "atType": 2,
                "message": [
                    {"type": "reply", "data": {"id": "1660339207"}},
                    {"type": "text", "data": {"text": "@测试机器人 你也做一个一样的群总结，根据历史群消息"}},
                ],
            }
        )

        self.assertEqual(
            [("qq:group:20002", "你也做一个一样的群总结，根据历史群消息")],
            bridge.submitted,
        )

    def test_group_text_name_only_mention_replies_hello_without_agent_submission(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge._login_nickname = "测试机器人"
        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 20002,
                "user_id": 10001,
                "self_id": 900000001,
                "atType": 2,
                "message": [
                    {"type": "text", "data": {"text": "@测试机器人"}},
                ],
            }
        )

        self.assertEqual([], bridge.submitted)
        sent_messages = [payload["message"] for action, payload in bridge.api_calls if action == "send_group_msg"]
        self.assertEqual(["你好"], sent_messages)

    def test_group_text_name_mention_without_napcat_at_type_is_ignored(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge._login_nickname = "测试机器人"
        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 20002,
                "user_id": 10001,
                "self_id": 900000001,
                "message": [
                    {"type": "text", "data": {"text": "@测试机器人 这只是普通文本"}},
                ],
            }
        )

        self.assertEqual([], bridge.submitted)

    def test_group_empty_mention_replies_hello_without_agent_submission(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 20002,
                "user_id": 10001,
                "self_id": 900000001,
                "message": [
                    {"type": "at", "data": {"qq": "900000001"}},
                ],
            }
        )

        self.assertEqual([], bridge.submitted)
        sent_messages = [payload["message"] for action, payload in bridge.api_calls if action == "send_group_msg"]
        self.assertEqual(["你好"], sent_messages)

    def test_group_interrupt_suppresses_notice_but_resubmits_latest_message(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.config.qq_group_require_mention = False
        bridge.interrupt_runtime.delay_seconds = 0.08
        bridge.pending_tasks = {
            "task-old": BridgePendingReplyTask(
                task_id="task-old",
                sender_key="qq:group:20002",
                reply_target={"message_type": "group", "group_id": 20002},
                created_at=123,
                interrupt_base_prompt="原始问题",
            )
        }

        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 20002,
                "user_id": 10001,
                "message": "新的补充",
            }
        )

        self.assertNotIn("task-old", bridge.pending_tasks)
        time.sleep(0.2)
        self.assertEqual([("qq:group:20002", "新的补充")], bridge.submitted)
        sent_messages = [payload["message"] for action, payload in bridge.api_calls if action == "send_group_msg"]
        self.assertEqual([], sent_messages)

    def test_group_repeated_interrupt_targets_latest_speaker(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.config.qq_group_require_mention = False
        bridge.interrupt_runtime.delay_seconds = 0.08
        bridge.pending_tasks = {
            "task-old": BridgePendingReplyTask(
                task_id="task-old",
                sender_key="qq:group:20002",
                reply_target={"message_type": "group", "group_id": 20002, "user_id": 10001},
                created_at=123,
                interrupt_base_prompt="原始问题",
            )
        }

        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 20002,
                "user_id": 10001,
                "message": "第一次补充",
            }
        )
        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 20002,
                "user_id": 10002,
                "message": "第二次补充",
            }
        )

        time.sleep(0.2)

        self.assertIn("task-qq-001", bridge.pending_tasks)
        self.assertEqual(
            {"message_type": "group", "group_id": 20002, "user_id": 10002},
            bridge.pending_tasks["task-qq-001"].reply_target,
        )
        self.assertEqual([("qq:group:20002", "第一次补充\n\n第二次补充")], bridge.submitted)
        sent_messages = [payload["message"] for action, payload in bridge.api_calls if action == "send_group_msg"]
        self.assertEqual([], sent_messages)

    def test_private_message_is_ignored_when_private_disabled(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.config.qq_private_enabled = False
        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message": "不要处理私聊",
            }
        )

        self.assertEqual([], bridge.submitted)

    def test_group_message_is_ignored_when_group_disabled(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.config.qq_group_enabled = False
        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 20002,
                "user_id": 10001,
                "self_id": 900000001,
                "message": [
                    {"type": "at", "data": {"qq": "900000001"}},
                    {"type": "text", "data": {"text": "不要处理群聊"}},
                ],
            }
        )

        self.assertEqual([], bridge.submitted)

    def test_group_message_respects_group_allowlist(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.config.qq_allowed_group_ids = ["30003"]
        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 20002,
                "user_id": 10001,
                "self_id": 900000001,
                "message": [
                    {"type": "at", "data": {"qq": "900000001"}},
                    {"type": "text", "data": {"text": "群不在白名单"}},
                ],
            }
        )

        self.assertEqual([], bridge.submitted)

    def test_group_control_command_is_rejected_without_agent_submission(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 20002,
                "user_id": 10001,
                "self_id": 900000001,
                "message": [
                    {"type": "at", "data": {"qq": "900000001"}},
                    {"type": "text", "data": {"text": "/status"}},
                ],
            }
        )

        self.assertEqual([], bridge.submitted)
        self.assertEqual("send_group_msg", bridge.api_calls[-1][0])
        self.assertIn("群聊只支持普通问题", bridge.api_calls[-1][1]["message"])

    def test_group_session_commands_are_rejected_without_agent_submission(self) -> None:
        for command in ("/model gpt-5", "/backend claude"):
            with self.subTest(command=command):
                bridge = FakeQQBridge(self.temp_path)
                bridge.handle_event(
                    {
                        "post_type": "message",
                        "message_type": "group",
                        "group_id": 20002,
                        "user_id": 10001,
                        "self_id": 900000001,
                        "message": [
                            {"type": "at", "data": {"qq": "900000001"}},
                            {"type": "text", "data": {"text": command}},
                        ],
                    }
                )

                self.assertEqual([], bridge.submitted)
                self.assertEqual("send_group_msg", bridge.api_calls[-1][0])
                self.assertIn("群聊只支持普通问题", bridge.api_calls[-1][1]["message"])

    def test_qq_help_lists_model_and_backend_commands(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message": "/help",
            }
        )

        self.assertEqual([], bridge.submitted)
        sent_text = str(bridge.api_calls[-1][1]["message"])
        self.assertIn("/model <name>", sent_text)
        self.assertIn("/backend <backend>", sent_text)

    def test_private_message_respects_private_user_blocklist(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.config.qq_blocked_private_user_ids = ["10001"]
        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message": "用户被拉黑",
            }
        )

        self.assertEqual([], bridge.submitted)

    def test_status_command_is_handled_locally_without_agent_submission(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.ipc_responses["state"] = SimpleNamespace(
            ok=True,
            error="",
            payload={
                "tasks": [
                    {
                        "id": "task-qq-latest",
                        "agent_id": "qq",
                        "agent_name": "QQ 会话",
                        "backend": "codex",
                        "source": "qq",
                        "sender_id": "qq:private:10001",
                        "prompt": "hello",
                        "status": "succeeded",
                        "created_at": "2026-06-26T01:00:00",
                        "started_at": "2026-06-26T01:00:01",
                        "finished_at": "2026-06-26T01:00:02",
                        "output": "ok",
                        "error": "",
                        "session_name": "qq-private-10001",
                    }
                ]
            },
        )

        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message": "/status",
            }
        )

        self.assertEqual([], bridge.submitted)
        self.assertEqual("send_private_msg", bridge.api_calls[-1][0])
        sent_text = str(bridge.api_calls[-1][1]["message"])
        self.assertIn("当前设置", sent_text)
        self.assertIn("task-qq-latest", sent_text)

    def test_control_command_entrypoint_routes_to_qq_command_router(self) -> None:
        bridge = FakeQQBridge(self.temp_path)

        reply, handled = bridge._handle_control_command("qq:private:10001", "/help")

        self.assertTrue(handled)
        self.assertIn("/model", reply)

    def test_clear_command_clears_resolved_qq_agent_session_file(self) -> None:
        session_dir = self.temp_path / "sessions"
        session_dir.mkdir()
        session_file = session_dir / "qq__qq-private-10001.txt"
        stale_file = session_dir / "qq-private-10001.jsonl"
        session_file.write_text("codex-session-id", encoding="utf-8")
        stale_file.write_text("old-unused-session-id", encoding="utf-8")

        bridge = FakeQQBridge(self.temp_path)

        with (
            patch("qq_onebot_bridge.SESSION_DIR", session_dir),
            patch.object(QQOneBotBridge, "_load_agents", return_value=[SimpleNamespace(id="qq", backend="codex", session_file=str(session_dir / "qq.txt"))]),
        ):
            bridge.handle_event(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "user_id": 10001,
                    "message": "/clear",
                }
            )

        self.assertEqual("", session_file.read_text(encoding="utf-8"))
        self.assertEqual("old-unused-session-id", stale_file.read_text(encoding="utf-8"))
        binding = bridge.conversations["qq:private:10001"]
        self.assertEqual("qq-private-10001-2", binding.current_session)
        self.assertEqual("qq-private-10001-2", binding.last_regular_session)
        self.assertIn("qq-private-10001", binding.sessions)
        self.assertIn("qq-private-10001-2", binding.sessions)
        self.assertEqual("qq-private-10001-2", bridge._session_name("qq:private:10001"))
        self.assertEqual("send_private_msg", bridge.api_calls[-1][0])
        self.assertIn("已清空当前 Agent 会话", bridge.api_calls[-1][1]["message"])
        self.assertIn("新会话: qq-private-10001-2", bridge.api_calls[-1][1]["message"])

    def test_clear_command_rotates_ui_session_when_backend_session_is_already_empty(self) -> None:
        session_dir = self.temp_path / "sessions"
        session_dir.mkdir()
        bridge = FakeQQBridge(self.temp_path)

        with (
            patch("qq_onebot_bridge.SESSION_DIR", session_dir),
            patch.object(QQOneBotBridge, "_load_agents", return_value=[SimpleNamespace(id="qq", backend="codex", session_file=str(session_dir / "qq.txt"))]),
        ):
            bridge.handle_event(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "user_id": 10001,
                    "message": "/clear",
                }
            )

        binding = bridge.conversations["qq:private:10001"]
        self.assertEqual("qq-private-10001-2", binding.current_session)
        self.assertIn("已经是空的，已切换到新会话", bridge.api_calls[-1][1]["message"])

    def test_restart_command_schedules_qq_stack_restart(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        with patch("qq_onebot_bridge.schedule_named_action", return_value=SimpleNamespace(message="scheduled qq stack")) as mocked_schedule:
            bridge.handle_event(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "user_id": 10001,
                    "message": "/restart",
                }
            )

        self.assertEqual([], bridge.submitted)
        self.assertEqual("send_private_msg", bridge.api_calls[-1][0])
        self.assertEqual("scheduled qq stack", bridge.api_calls[-1][1]["message"])
        mocked_schedule.assert_called_once_with("restart-qq-stack", delay_seconds=1.0)

    def test_restart_qq_command_schedules_qq_stack_restart(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        with patch("qq_onebot_bridge.schedule_named_action", return_value=SimpleNamespace(message="scheduled qq stack")) as mocked_schedule:
            bridge.handle_event(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "user_id": 10001,
                    "message": "/restart qq",
                }
            )

        self.assertEqual([], bridge.submitted)
        self.assertEqual("send_private_msg", bridge.api_calls[-1][0])
        self.assertEqual("scheduled qq stack", bridge.api_calls[-1][1]["message"])
        mocked_schedule.assert_called_once_with("restart-qq-stack", delay_seconds=1.0)

    def test_restart_bridge_command_schedules_qq_bridge_restart(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        with patch("qq_onebot_bridge.schedule_named_action", return_value=SimpleNamespace(message="scheduled qq bridge")) as mocked_schedule:
            bridge.handle_event(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "user_id": 10001,
                    "message": "/restart bridge",
                }
            )

        self.assertEqual([], bridge.submitted)
        self.assertEqual("send_private_msg", bridge.api_calls[-1][0])
        self.assertEqual("scheduled qq bridge", bridge.api_calls[-1][1]["message"])
        mocked_schedule.assert_called_once_with("restart-qq-bridge", delay_seconds=1.0)

    def test_restart_onebot_command_schedules_onebot_restart(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        with patch("qq_onebot_bridge.schedule_named_action", return_value=SimpleNamespace(message="scheduled onebot")) as mocked_schedule:
            bridge.handle_event(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "user_id": 10001,
                    "message": "/restart onebot",
                }
            )

        self.assertEqual([], bridge.submitted)
        self.assertEqual("send_private_msg", bridge.api_calls[-1][0])
        self.assertEqual("scheduled onebot", bridge.api_calls[-1][1]["message"])
        mocked_schedule.assert_called_once_with("restart-onebot-runtime", delay_seconds=1.0)

    def test_sendfile_command_uploads_private_file_without_agent_submission(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        target_file = self.temp_path / "report.pdf"
        target_file.write_bytes(b"pdf-data")
        with patch.object(bridge, "_resolve_shareable_project_file", return_value=target_file):
            bridge.handle_event(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "user_id": 10001,
                    "message": "/sendfile docs/report.pdf",
                }
            )

        self.assertEqual([], bridge.submitted)
        self.assertEqual("upload_private_file", bridge.api_calls[-1][0])
        self.assertEqual(10001, bridge.api_calls[-1][1]["user_id"])
        self.assertEqual(str(target_file), bridge.api_calls[-1][1]["file"])

    def test_sendfile_command_sends_private_image_as_image_message(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        target_file = self.temp_path / "diagram.png"
        target_file.write_bytes(b"png-data")
        with patch.object(bridge, "_resolve_shareable_project_file", return_value=target_file):
            bridge.handle_event(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "user_id": 10001,
                    "message": "/sendfile docs/diagram.png",
                }
            )

        self.assertEqual([], bridge.submitted)
        self.assertEqual("send_private_msg", bridge.api_calls[-1][0])
        self.assertEqual(10001, bridge.api_calls[-1][1]["user_id"])
        self.assertEqual([{"type": "image", "data": {"file": str(target_file)}}], bridge.api_calls[-1][1]["message"])

    def test_sendfile_command_reports_failed_image_send(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        target_file = self.temp_path / "diagram.png"
        target_file.write_bytes(b"png-data")

        def fake_onebot_api(action: str, payload: dict[str, object]) -> dict[str, object]:
            bridge.api_calls.append((action, payload))
            if payload.get("message") == [{"type": "image", "data": {"file": str(target_file)}}]:
                return {"status": "failed", "retcode": 200, "message": "rich media transfer failed"}
            return {"status": "ok", "retcode": 0}

        bridge._onebot_api = fake_onebot_api  # type: ignore[method-assign]
        with patch.object(bridge, "_resolve_shareable_project_file", return_value=target_file):
            bridge.handle_event(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "user_id": 10001,
                    "message": "/sendfile docs/diagram.png",
                }
            )

        self.assertEqual([], bridge.submitted)
        self.assertEqual("send_private_msg", bridge.api_calls[-1][0])
        self.assertIn("rich media transfer failed", bridge.api_calls[-1][1]["message"])

    def test_showfile_command_denies_sensitive_paths_like_weixin(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message": "/showfile accounts/wechat-bot.json",
            }
        )

        self.assertEqual([], bridge.submitted)
        self.assertEqual("send_private_msg", bridge.api_calls[-1][0])
        self.assertIn("拒绝预览文件", bridge.api_calls[-1][1]["message"])

    def test_double_slash_status_queries_codex_status_without_agent_submission(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.ipc_responses["codex_status"] = SimpleNamespace(ok=True, error="", payload={"status": "Codex status panel"})

        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message": "//status",
            }
        )

        self.assertEqual([], bridge.submitted)
        self.assertEqual("send_private_msg", bridge.api_calls[-1][0])
        self.assertEqual("Codex status panel", bridge.api_calls[-1][1]["message"])

        status_call = next(payload for action, payload in bridge.api_calls if action == "ipc:codex_status")
        self.assertEqual(DEFAULT_QQ_AGENT_ID, status_call["agent_id"])
        self.assertEqual("qq-private-10001", status_call["session_name"])
        self.assertEqual(str(WORKSPACE_DIR.resolve()), status_call["workdir"])

    def test_double_slash_status_bypasses_active_model_menu(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.ipc_responses["codex_status"] = SimpleNamespace(ok=True, error="", payload={"status": "Codex status panel"})

        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message": "//model",
            }
        )
        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message": "//status",
            }
        )

        self.assertEqual([], bridge.submitted)
        self.assertEqual("Codex status panel", bridge.api_calls[-1][1]["message"])
        session = bridge.conversations["qq:private:10001"].sessions["qq-private-10001"]
        self.assertEqual("/model", session.native_menu_command)

    def test_double_slash_status_timeout_returns_a_reply(self) -> None:
        bridge = FakeQQBridge(self.temp_path)

        with patch.object(bridge, "_ipc_request", side_effect=TimeoutError("status query timed out")):
            bridge.handle_event(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "user_id": 10001,
                    "message": "//status",
                }
            )

        self.assertEqual([], bridge.submitted)
        self.assertEqual("send_private_msg", bridge.api_calls[-1][0])
        self.assertIn("Codex 状态查询失败", bridge.api_calls[-1][1]["message"])

    def test_group_double_slash_status_is_rejected(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 20002,
                "user_id": 10001,
                "self_id": 900000001,
                "message": [
                    {"type": "at", "data": {"qq": "900000001"}},
                    {"type": "text", "data": {"text": "//status"}},
                ],
            }
        )

        self.assertEqual([], bridge.submitted)
        self.assertEqual("send_group_msg", bridge.api_calls[-1][0])
        self.assertIn("群聊只支持普通问题", bridge.api_calls[-1][1]["message"])

    def test_unknown_double_slash_command_still_passes_through_to_agent(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message": "//help",
            }
        )

        self.assertEqual(1, len(bridge.submitted))
        self.assertEqual("//help", bridge.submitted[0][1])

    def test_wait_and_reply_streams_progress_before_final_reply(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        responses = [
            SimpleNamespace(
                ok=True,
                payload={
                    "task": {
                        "id": "task-qq-001",
                        "agent_id": "qq",
                        "agent_name": "QQ 会话",
                        "backend": "codex",
                        "source": "qq",
                        "sender_id": "qq:private:10001",
                        "prompt": "hello",
                        "status": "running",
                        "created_at": "2026-06-26T01:00:00",
                        "started_at": "2026-06-26T01:00:01",
                        "finished_at": "",
                        "output": "",
                        "error": "",
                        "session_name": "qq-private-10001",
                        "progress_text": "正在检查项目",
                        "progress_at": "2026-06-26T01:00:02",
                        "progress_seq": 1,
                    }
                },
            ),
            SimpleNamespace(
                ok=True,
                payload={
                    "task": {
                        "id": "task-qq-001",
                        "agent_id": "qq",
                        "agent_name": "QQ 会话",
                        "backend": "codex",
                        "source": "qq",
                        "sender_id": "qq:private:10001",
                        "prompt": "hello",
                        "status": "succeeded",
                        "created_at": "2026-06-26T01:00:00",
                        "started_at": "2026-06-26T01:00:01",
                        "finished_at": "2026-06-26T01:00:05",
                        "output": "检查完成",
                        "error": "",
                        "session_name": "qq-private-10001",
                        "progress_text": "正在检查项目",
                        "progress_at": "2026-06-26T01:00:02",
                        "progress_seq": 1,
                    }
                },
            ),
        ]

        context_response = SimpleNamespace(ok=True, payload={"context_left_percent": 42})
        with patch("qq_onebot_bridge.create_request", return_value="req-get-task"), patch("qq_onebot_bridge.wait_for_response", side_effect=responses), patch.object(
            bridge,
            "_ipc_request",
            return_value=context_response,
        ):
            QQOneBotBridge._wait_and_reply(bridge, {"message_type": "private", "user_id": 10001}, "task-qq-001")

        sent_messages = [payload["message"] for action, payload in bridge.api_calls if action == "send_private_msg"]
        self.assertEqual(2, len(sent_messages))
        self.assertIn("正在检查项目", sent_messages[0])
        self.assertIn("ctx 42%", sent_messages[0].splitlines()[0])
        self.assertIn("检查完成", sent_messages[1])
        self.assertIn("ctx 42%", sent_messages[1].splitlines()[0])
        typing_calls = [payload for action, payload in bridge.api_calls if action == "set_input_status"]
        self.assertEqual([1, 0], [payload["event_type"] for payload in typing_calls])

    def test_submitted_task_is_persisted_for_restart_recovery(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message": "hello",
            }
        )

        self.assertIn("task-qq-001", bridge.pending_tasks)
        persisted = bridge.pending_tasks["task-qq-001"]
        self.assertEqual("qq:private:10001", persisted.sender_key)
        self.assertEqual({"message_type": "private", "user_id": 10001}, persisted.reply_target)

    def test_recover_pending_tasks_reconciles_once_after_bridge_restart(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.pending_tasks = {
            "task-qq-001": BridgePendingReplyTask(
                task_id="task-qq-001",
                sender_key="qq:private:10001",
                reply_target={"message_type": "private", "user_id": 10001},
                created_at=123,
                last_progress_seq=2,
                last_progress_text="已经发送过的进度",
            )
        }
        task = HubTask.from_dict(
            {
                "id": "task-qq-001",
                "agent_id": "qq",
                "agent_name": "QQ 会话",
                "backend": "codex",
                "source": "qq",
                "sender_id": "qq:private:10001",
                "prompt": "hello",
                "status": "succeeded",
                "created_at": "2026-06-26T01:00:00",
                "started_at": "2026-06-26T01:00:01",
                "finished_at": "2026-06-26T01:00:05",
                "output": "恢复后完成",
                "error": "",
                "session_name": "qq-private-10001",
                "progress_text": "已经发送过的进度",
                "progress_at": "2026-06-26T01:00:02",
                "progress_seq": 2,
            },
            default_backend="codex",
        )

        with patch.object(bridge, "_start_thread", side_effect=lambda _name, target, *args: target(*args)), patch.object(
            bridge,
            "_get_task_for_delivery",
            return_value=task,
        ):
            bridge.recover_pending_tasks()

        self.assertNotIn("task-qq-001", bridge.pending_tasks)
        sent_messages = [payload["message"] for action, payload in bridge.api_calls if action == "send_private_msg"]
        self.assertIn("恢复后完成", sent_messages[-1])

    def test_unknown_after_restart_task_is_resubmitted(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.pending_tasks = {
            "task-qq-001": BridgePendingReplyTask(
                task_id="task-qq-001",
                sender_key="qq:private:10001",
                reply_target={"message_type": "private", "user_id": 10001},
                created_at=123,
                interrupt_base_prompt="继续完成重构",
            )
        }
        task = HubTask.from_dict(
            {
                "id": "task-qq-001",
                "agent_id": "qq",
                "agent_name": "QQ 会话",
                "backend": "codex",
                "source": "qq",
                "sender_id": "qq:private:10001",
                "prompt": "继续完成重构",
                "status": "unknown_after_restart",
                "created_at": "2026-06-26T01:00:00",
                "finished_at": "2026-06-26T01:00:05",
                "error": "Hub restarted while this task was running.",
                "session_name": "qq-private-10001",
            },
            default_backend="codex",
        )

        with patch.object(bridge, "_submit_task", return_value={"id": "task-qq-resumed"}):
            bridge._handle_pushed_task_update({"task": task.to_dict()})

        self.assertNotIn("task-qq-001", bridge.pending_tasks)
        self.assertIn("task-qq-resumed", bridge.pending_tasks)
        self.assertEqual(1, bridge.pending_tasks["task-qq-resumed"].restart_resume_count)
        sent_messages = [payload["message"] for action, payload in bridge.api_calls if action == "send_private_msg"]
        self.assertIn("Hub 已重启", sent_messages[-1])

    def test_group_unknown_after_restart_resubmits_without_group_notice(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.pending_tasks = {
            "task-qq-group": BridgePendingReplyTask(
                task_id="task-qq-group",
                sender_key="qq:group:20002",
                reply_target={"message_type": "group", "group_id": 20002, "user_id": 10001},
                created_at=123,
                interrupt_base_prompt="你好",
            )
        }
        task = HubTask.from_dict(
            {
                "id": "task-qq-group",
                "agent_id": "qq-group",
                "agent_name": "QQ 群聊",
                "backend": "codex",
                "source": "qq",
                "sender_id": "qq:group:20002",
                "prompt": "你好",
                "status": "unknown_after_restart",
                "created_at": "2026-06-26T01:00:00",
                "finished_at": "2026-06-26T01:00:05",
                "error": "Hub restarted while this task was running.",
                "session_name": "qq-group-20002",
            },
            default_backend="codex",
        )

        with patch.object(bridge, "_submit_task", return_value={"id": "task-qq-resumed"}):
            bridge._handle_pushed_task_update({"task": task.to_dict()})

        self.assertNotIn("task-qq-group", bridge.pending_tasks)
        self.assertIn("task-qq-resumed", bridge.pending_tasks)
        sent_messages = [payload["message"] for action, payload in bridge.api_calls if action == "send_group_msg"]
        self.assertEqual([], sent_messages)

    def test_cancel_failure_keeps_pending_task_for_restart_resume(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.pending_tasks = {
            "task-qq-001": BridgePendingReplyTask(
                task_id="task-qq-001",
                sender_key="qq:private:10001",
                reply_target={"message_type": "private", "user_id": 10001},
                created_at=123,
            )
        }

        with patch.object(bridge, "_ipc_request", side_effect=TimeoutError("hub down")):
            bridge._cancel_task_best_effort("task-qq-001")

        self.assertIn("task-qq-001", bridge.pending_tasks)

    def test_cancel_rejection_keeps_pending_task_for_restart_resume(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.pending_tasks = {
            "task-qq-001": BridgePendingReplyTask(
                task_id="task-qq-001",
                sender_key="qq:private:10001",
                reply_target={"message_type": "private", "user_id": 10001},
                created_at=123,
            )
        }
        bridge.ipc_responses["cancel_task"] = SimpleNamespace(ok=False, error="hub busy", payload={})

        bridge._cancel_task_best_effort("task-qq-001")

        self.assertIn("task-qq-001", bridge.pending_tasks)

    def test_cancel_success_removes_pending_task(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.pending_tasks = {
            "task-qq-001": BridgePendingReplyTask(
                task_id="task-qq-001",
                sender_key="qq:private:10001",
                reply_target={"message_type": "private", "user_id": 10001},
                created_at=123,
            )
        }

        bridge._cancel_task_best_effort("task-qq-001")

        self.assertNotIn("task-qq-001", bridge.pending_tasks)

    def test_pushed_task_update_streams_progress_and_final_reply(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.pending_tasks = {
            "task-qq-001": BridgePendingReplyTask(
                task_id="task-qq-001",
                sender_key="qq:private:10001",
                reply_target={"message_type": "private", "user_id": 10001},
                created_at=123,
            )
        }
        base_task = {
            "id": "task-qq-001",
            "agent_id": "qq",
            "agent_name": "QQ 会话",
            "backend": "codex",
            "source": "qq",
            "sender_id": "qq:private:10001",
            "prompt": "hello",
            "created_at": "2026-06-26T01:00:00",
            "started_at": "2026-06-26T01:00:01",
            "session_name": "qq-private-10001",
            "workdir": "",
            "progress_at": "2026-06-26T01:00:02",
        }

        bridge._handle_pushed_task_update({"task": {**base_task, "status": "running", "progress_text": "正在处理 QQ 消息", "progress_seq": 1}})
        bridge._handle_pushed_task_update(
            {
                "task": {
                    **base_task,
                    "status": "succeeded",
                    "finished_at": "2026-06-26T01:00:05",
                    "output": "QQ 消息完成",
                    "error": "",
                    "progress_text": "正在处理 QQ 消息",
                    "progress_seq": 1,
                }
            }
        )

        self.assertNotIn("task-qq-001", bridge.pending_tasks)
        sent_messages = [payload["message"] for action, payload in bridge.api_calls if action == "send_private_msg"]
        self.assertEqual(2, len(sent_messages))
        self.assertIn("正在处理 QQ 消息", sent_messages[0])
        self.assertIn("QQ 消息完成", sent_messages[1])

    def test_pushed_task_update_keeps_done_status_when_final_matches_progress(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.pending_tasks = {
            "task-qq-001": BridgePendingReplyTask(
                task_id="task-qq-001",
                sender_key="qq:private:10001",
                reply_target={"message_type": "private", "user_id": 10001},
                created_at=123,
            )
        }
        base_task = {
            "id": "task-qq-001",
            "agent_id": "qq",
            "agent_name": "QQ 会话",
            "backend": "codex",
            "source": "qq",
            "sender_id": "qq:private:10001",
            "prompt": "hello",
            "created_at": "2026-06-26T01:00:00",
            "started_at": "2026-06-26T01:00:01",
            "session_name": "qq-private-10001",
            "workdir": "",
            "progress_at": "2026-06-26T01:00:02",
        }

        bridge._handle_pushed_task_update({"task": {**base_task, "status": "running", "progress_text": "这是最终回答", "progress_seq": 1}})
        bridge._handle_pushed_task_update(
            {
                "task": {
                    **base_task,
                    "status": "succeeded",
                    "finished_at": "2026-06-26T01:00:05",
                    "output": "这是最终回答",
                    "error": "",
                    "progress_text": "这是最终回答",
                    "progress_seq": 1,
                }
            }
        )

        sent_messages = [payload["message"] for action, payload in bridge.api_calls if action == "send_private_msg"]
        self.assertEqual(2, len(sent_messages))
        self.assertTrue(sent_messages[0].startswith("running · "))
        self.assertIn("\n\n这是最终回答", sent_messages[0])
        self.assertTrue(sent_messages[1].startswith("done · "))
        self.assertNotIn("\n\n", sent_messages[1])

    def test_group_pushed_terminal_success_sends_plain_answer_without_done_metadata(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.pending_tasks = {
            "task-qq-group": BridgePendingReplyTask(
                task_id="task-qq-group",
                sender_key="qq:group:20002",
                reply_target={"message_type": "group", "group_id": 20002, "user_id": 10001},
                created_at=123,
            )
        }

        bridge._handle_pushed_task_update(
            {
                "task": {
                    "id": "task-qq-group",
                    "agent_id": "qq-group",
                    "agent_name": "QQ 群聊",
                    "backend": "codex",
                    "source": "qq",
                    "sender_id": "qq:group:20002",
                    "prompt": "hello",
                    "status": "succeeded",
                    "created_at": "2026-06-26T01:00:00",
                    "started_at": "2026-06-26T01:00:01",
                    "finished_at": "2026-06-26T01:00:05",
                    "output": "群聊回答",
                    "error": "",
                    "session_name": "qq-group-20002",
                    "progress_text": "",
                    "progress_at": "",
                    "progress_seq": 0,
                }
            }
        )

        self.assertNotIn("task-qq-group", bridge.pending_tasks)
        sent_messages = [payload["message"] for action, payload in bridge.api_calls if action == "send_group_msg"]
        self.assertEqual(["[CQ:at,qq=10001] 群聊回答"], sent_messages)

    def test_group_pushed_progress_is_suppressed_and_final_answer_is_not_deduped(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.pending_tasks = {
            "task-qq-group": BridgePendingReplyTask(
                task_id="task-qq-group",
                sender_key="qq:group:20002",
                reply_target={"message_type": "group", "group_id": 20002, "user_id": 10001},
                created_at=123,
            )
        }
        base_task = {
            "id": "task-qq-group",
            "agent_id": "qq-group",
            "agent_name": "QQ 群聊",
            "backend": "codex",
            "source": "qq",
            "sender_id": "qq:group:20002",
            "prompt": "hello",
            "created_at": "2026-06-26T01:00:00",
            "started_at": "2026-06-26T01:00:01",
            "session_name": "qq-group-20002",
            "progress_at": "2026-06-26T01:00:02",
        }

        bridge._handle_pushed_task_update(
            {
                "task": {
                    **base_task,
                    "status": "running",
                    "progress_text": "群聊回答",
                    "progress_seq": 1,
                }
            }
        )
        bridge._handle_pushed_task_update(
            {
                "task": {
                    **base_task,
                    "status": "succeeded",
                    "finished_at": "2026-06-26T01:00:05",
                    "output": "群聊回答",
                    "error": "",
                    "progress_text": "群聊回答",
                    "progress_seq": 1,
                }
            }
        )

        self.assertNotIn("task-qq-group", bridge.pending_tasks)
        sent_messages = [payload["message"] for action, payload in bridge.api_calls if action == "send_group_msg"]
        self.assertEqual(["[CQ:at,qq=10001] 群聊回答"], sent_messages)

    def test_group_pushed_terminal_failure_is_silent(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.pending_tasks = {
            "task-qq-group": BridgePendingReplyTask(
                task_id="task-qq-group",
                sender_key="qq:group:20002",
                reply_target={"message_type": "group", "group_id": 20002},
                created_at=123,
            )
        }

        bridge._handle_pushed_task_update(
            {
                "task": {
                    "id": "task-qq-group",
                    "agent_id": "qq-group",
                    "agent_name": "QQ 群聊",
                    "backend": "codex",
                    "source": "qq",
                    "sender_id": "qq:group:20002",
                    "prompt": "hello",
                    "status": "failed",
                    "created_at": "2026-06-26T01:00:00",
                    "started_at": "2026-06-26T01:00:01",
                    "finished_at": "2026-06-26T01:00:05",
                    "output": "",
                    "error": "local permission denied",
                    "session_name": "qq-group-20002",
                    "progress_text": "",
                    "progress_at": "",
                    "progress_seq": 0,
                }
            }
        )

        self.assertNotIn("task-qq-group", bridge.pending_tasks)
        sent_messages = [payload["message"] for action, payload in bridge.api_calls if action == "send_group_msg"]
        self.assertEqual([], sent_messages)

    def test_process_bridge_ipc_consumes_qq_channel_update(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.pending_tasks = {
            "task-qq-001": BridgePendingReplyTask(
                task_id="task-qq-001",
                sender_key="qq:private:10001",
                reply_target={"message_type": "private", "user_id": 10001},
                created_at=123,
            )
        }
        ipc_root = self.temp_path / "ipc"
        patchers = [
            patch("local_ipc.IPC_DIR", ipc_root),
            patch("local_ipc.REQUEST_DIR", ipc_root / "requests"),
            patch("local_ipc.RESPONSE_DIR", ipc_root / "responses"),
            patch("local_ipc.PROCESSED_DIR", ipc_root / "processed"),
            patch("local_ipc.BRIDGE_REQUEST_DIR", ipc_root / "bridge_requests"),
            patch("local_ipc.BRIDGE_PROCESSED_DIR", ipc_root / "bridge_processed"),
            patch("local_ipc.BRIDGE_CHANNELS_DIR", ipc_root / "bridge_channels"),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        request_id = local_ipc.create_bridge_request(
            "task_update",
            {
                "task": {
                    "id": "task-qq-001",
                    "agent_id": "qq",
                    "agent_name": "QQ 会话",
                    "backend": "codex",
                    "source": "qq",
                    "sender_id": "qq:private:10001",
                    "prompt": "hello",
                    "status": "succeeded",
                    "created_at": "2026-06-26T01:00:00",
                    "started_at": "2026-06-26T01:00:01",
                    "finished_at": "2026-06-26T01:00:05",
                    "output": "频道事件完成",
                    "error": "",
                    "session_name": "qq-private-10001",
                    "progress_text": "",
                    "progress_at": "",
                    "progress_seq": 0,
                }
            },
            channel="qq",
        )

        bridge._process_bridge_ipc_once()

        self.assertFalse((local_ipc.bridge_request_dir("qq") / f"{request_id}.json").exists())
        self.assertTrue((local_ipc.bridge_processed_dir("qq") / f"{request_id}.json").exists())
        sent_messages = [payload["message"] for action, payload in bridge.api_calls if action == "send_private_msg"]
        self.assertIn("频道事件完成", sent_messages[-1])

    def test_wait_and_reply_removes_pending_task_after_terminal_reply(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.pending_tasks = {
            "task-qq-001": BridgePendingReplyTask(
                task_id="task-qq-001",
                sender_key="qq:private:10001",
                reply_target={"message_type": "private", "user_id": 10001},
                created_at=123,
            )
        }
        response = SimpleNamespace(
            ok=True,
            payload={
                "task": {
                    "id": "task-qq-001",
                    "agent_id": "qq",
                    "agent_name": "QQ 会话",
                    "backend": "codex",
                    "source": "qq",
                    "sender_id": "qq:private:10001",
                    "prompt": "hello",
                    "status": "succeeded",
                    "created_at": "2026-06-26T01:00:00",
                    "started_at": "2026-06-26T01:00:01",
                    "finished_at": "2026-06-26T01:00:05",
                    "output": "完成",
                    "error": "",
                    "session_name": "qq-private-10001",
                    "progress_text": "",
                    "progress_at": "",
                    "progress_seq": 0,
                }
            },
        )

        with patch("qq_onebot_bridge.create_request", return_value="req-get-task"), patch("qq_onebot_bridge.wait_for_response", return_value=response), patch.object(
            bridge,
            "_ipc_request",
            return_value=SimpleNamespace(ok=True, payload={}),
        ):
            QQOneBotBridge._wait_and_reply(bridge, {"message_type": "private", "user_id": 10001}, "task-qq-001")

        self.assertNotIn("task-qq-001", bridge.pending_tasks)
        sent_messages = [payload["message"] for action, payload in bridge.api_calls if action == "send_private_msg"]
        self.assertIn("完成", sent_messages[-1])

    def test_wait_and_reply_sends_only_incremental_progress_delta(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        base_task = {
            "id": "task-qq-001",
            "agent_id": "qq",
            "agent_name": "QQ 会话",
            "backend": "codex",
            "source": "qq",
            "sender_id": "qq:private:10001",
            "prompt": "hello",
            "created_at": "2026-06-26T01:00:00",
            "started_at": "2026-06-26T01:00:01",
            "finished_at": "",
            "output": "",
            "error": "",
            "session_name": "qq-private-10001",
            "progress_at": "2026-06-26T01:00:02",
        }
        responses = [
            SimpleNamespace(ok=True, payload={"task": {**base_task, "status": "running", "progress_text": "第一段内容完成", "progress_seq": 1}}),
            SimpleNamespace(ok=True, payload={"task": {**base_task, "status": "running", "progress_text": "第一段内容完成\n第二段内容完成", "progress_seq": 2}}),
            SimpleNamespace(
                ok=True,
                payload={
                    "task": {
                        **base_task,
                        "status": "succeeded",
                        "finished_at": "2026-06-26T01:00:05",
                        "output": "完成",
                        "progress_text": "第一段内容完成\n第二段内容完成",
                        "progress_seq": 2,
                    }
                },
            ),
        ]

        with patch("qq_onebot_bridge.create_request", return_value="req-get-task"), patch("qq_onebot_bridge.wait_for_response", side_effect=responses), patch.object(
            bridge,
            "_ipc_request",
            return_value=SimpleNamespace(ok=True, payload={}),
        ):
            QQOneBotBridge._wait_and_reply(bridge, {"message_type": "private", "user_id": 10001}, "task-qq-001")

        sent_messages = [payload["message"] for action, payload in bridge.api_calls if action == "send_private_msg"]
        self.assertIn("第一段内容完成", sent_messages[0])
        self.assertIn("第二段内容完成", sent_messages[1])
        self.assertNotIn("第一段内容完成", sent_messages[1])
        self.assertIn("完成", sent_messages[2])

    def test_wait_and_reply_sends_tiny_progress_fragments(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        base_task = {
            "id": "task-qq-001",
            "agent_id": "qq",
            "agent_name": "QQ 会话",
            "backend": "codex",
            "source": "qq",
            "sender_id": "qq:private:10001",
            "prompt": "hello",
            "created_at": "2026-06-26T01:00:00",
            "started_at": "2026-06-26T01:00:01",
            "finished_at": "",
            "output": "",
            "error": "",
            "session_name": "qq-private-10001",
            "progress_at": "2026-06-26T01:00:02",
        }
        responses = [
            SimpleNamespace(ok=True, payload={"task": {**base_task, "status": "running", "progress_text": "刚", "progress_seq": 1}}),
            SimpleNamespace(ok=True, payload={"task": {**base_task, "status": "running", "progress_text": "刚开始分析项目", "progress_seq": 2}}),
            SimpleNamespace(ok=True, payload={"task": {**base_task, "status": "succeeded", "finished_at": "2026-06-26T01:00:05", "output": "完成", "progress_text": "刚开始分析项目", "progress_seq": 2}}),
        ]

        with patch("qq_onebot_bridge.create_request", return_value="req-get-task"), patch("qq_onebot_bridge.wait_for_response", side_effect=responses), patch.object(
            bridge,
            "_ipc_request",
            return_value=SimpleNamespace(ok=True, payload={}),
        ):
            QQOneBotBridge._wait_and_reply(bridge, {"message_type": "private", "user_id": 10001}, "task-qq-001")

        sent_messages = [payload["message"] for action, payload in bridge.api_calls if action == "send_private_msg"]
        self.assertEqual(3, len(sent_messages))
        self.assertEqual("刚", sent_messages[0].split("\n\n", 1)[-1])
        self.assertIn("开始分析项目", sent_messages[1])
        self.assertNotIn("刚", sent_messages[1].split("\n\n", 1)[-1])
        self.assertIn("完成", sent_messages[2])

    def test_group_message_at_other_user_is_ignored(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 20002,
                "user_id": 10001,
                "self_id": 900000001,
                "message": [
                    {"type": "at", "data": {"qq": "10002"}},
                    {"type": "text", "data": {"text": "也不要回复这条"}},
                ],
            }
        )

        self.assertEqual([], bridge.submitted)

    def test_send_reply_uses_private_or_group_api(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge._send_reply({"message_type": "private", "user_id": 10001}, "hello")
        bridge._send_reply({"message_type": "group", "group_id": 20002}, "world")
        bridge._send_reply({"message_type": "group", "group_id": 20002, "user_id": 10002}, "answer")

        self.assertEqual(
            [
                ("send_private_msg", {"user_id": 10001, "message": "hello"}),
                ("send_group_msg", {"group_id": 20002, "message": "world"}),
                ("send_group_msg", {"group_id": 20002, "message": "answer"}),
            ],
            bridge.api_calls,
        )

    def test_reply_target_from_sender_key_supports_group_session_formats(self) -> None:
        self.assertEqual(
            {"message_type": "group", "group_id": "20002"},
            QQOneBotBridge._reply_target_from_sender_key("qq:group:20002"),
        )
        self.assertEqual(
            {"message_type": "group", "group_id": "20002", "user_id": "10001"},
            QQOneBotBridge._reply_target_from_sender_key("qq:group:20002:10001"),
        )

    def test_submit_task_uses_dedicated_qq_agent_and_sender_session(self) -> None:
        bridge = QQOneBotBridge(
            SimpleNamespace(backend_id="main", default_backend="codex", hub_task_timeout_seconds=30),
            api_base="http://onebot.local",
        )
        calls: list[tuple[str, dict[str, object]]] = []

        def fake_create_request(action: str, payload: dict[str, object]) -> str:
            calls.append((action, payload))
            return "req-qq-001"

        response = SimpleNamespace(ok=True, error="", payload={"task": {"id": "task-qq-001"}})
        with patch("qq_onebot_bridge.create_request", side_effect=fake_create_request), patch("qq_onebot_bridge.wait_for_response", return_value=response):
            task = bridge._submit_task("qq:private:10001", "hello")

        self.assertEqual({"id": "task-qq-001"}, task)
        self.assertEqual("submit_task", calls[0][0])
        self.assertEqual(DEFAULT_QQ_AGENT_ID, calls[0][1]["agent_id"])
        self.assertEqual("qq-private-10001", calls[0][1]["session_name"])
        self.assertEqual("qq", calls[0][1]["source"])

    def test_group_submit_task_uses_group_agent_restricted_permission_and_search(self) -> None:
        bridge = QQOneBotBridge(
            SimpleNamespace(backend_id="main", default_backend="codex", hub_task_timeout_seconds=30, language="zh-CN"),
            api_base="http://onebot.local",
        )
        calls: list[tuple[str, dict[str, object]]] = []

        def fake_create_request(action: str, payload: dict[str, object]) -> str:
            calls.append((action, payload))
            return "req-qq-group"

        response = SimpleNamespace(ok=True, error="", payload={"task": {"id": "task-qq-group"}})
        with patch("qq_onebot_bridge.create_request", side_effect=fake_create_request), patch("qq_onebot_bridge.wait_for_response", return_value=response):
            task = bridge._submit_task("qq:group:20002", "总结今天的公开新闻")

        self.assertEqual({"id": "task-qq-group"}, task)
        payload = calls[0][1]
        self.assertEqual("qq-group", payload["agent_id"])
        self.assertEqual("read-only", payload["permission_mode"])
        self.assertEqual("qq_group", payload["permission_profile"])
        self.assertIs(True, payload["codex_search_enabled"])
        self.assertEqual("qq-group-20002", payload["session_name"])
        self.assertEqual("总结今天的公开新闻", payload["prompt"])

    def test_group_submit_ignores_session_model_override(self) -> None:
        bridge = QQOneBotBridge(
            SimpleNamespace(backend_id="main", default_backend="codex", hub_task_timeout_seconds=30, language="zh-CN"),
            api_base="http://onebot.local",
        )
        message = IncomingBridgeMessage(
            sender_id="qq:group:20002",
            text="hello",
            reply_target={"message_type": "group", "group_id": 20002},
            source="qq",
            session_name="qq-group-20002",
            attachments=[],
            attachment_errors=[],
        )
        session = {
            "session_name": "qq-group-20002",
            "session_meta": SimpleNamespace(
                backend="codex",
                workdir="",
                model="gpt-5",
                reasoning_effort="ultra",
                permission_mode="",
            ),
        }

        context = bridge._resolve_task_submit_context(message, session)

        self.assertEqual("qq-group", context.agent_id)
        self.assertEqual("", context.model)
        self.assertEqual("", context.reasoning_effort)

    def test_private_submit_keeps_session_model_override(self) -> None:
        bridge = QQOneBotBridge(
            SimpleNamespace(backend_id="main", default_backend="codex", hub_task_timeout_seconds=30, language="zh-CN"),
            api_base="http://onebot.local",
        )
        message = IncomingBridgeMessage(
            sender_id="qq:private:10001",
            text="hello",
            reply_target={"message_type": "private", "user_id": 10001},
            source="qq",
            session_name="qq-private-10001",
            attachments=[],
            attachment_errors=[],
        )
        session = {
            "session_name": "qq-private-10001",
            "session_meta": SimpleNamespace(
                backend="codex",
                workdir="",
                model="gpt-5.6-sol",
                reasoning_effort="ultra",
                permission_mode="",
            ),
        }

        context = bridge._resolve_task_submit_context(message, session)

        self.assertEqual("gpt-5.6-sol", context.model)
        self.assertEqual("ultra", context.reasoning_effort)

    def test_group_image_submit_uses_permission_profile_and_codex_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_image = temp_path / "onebot-cache-image.png"
            source_image.write_bytes(b"png-data")
            group_workdir = temp_path / "qq-group-workspace"
            bridge = QQOneBotBridge(
                SimpleNamespace(backend_id="main", default_backend="codex", hub_task_timeout_seconds=30, language="zh-CN"),
                api_base="http://onebot.local",
            )
            calls: list[tuple[str, dict[str, object]]] = []

            def fake_create_request(action: str, payload: dict[str, object]) -> str:
                calls.append((action, payload))
                return "req-qq-group-image"

            response = SimpleNamespace(ok=True, error="", payload={"task": {"id": "task-qq-group-image"}})
            with (
                patch.object(QQOneBotBridge, "_load_agents", return_value=[SimpleNamespace(id="qq-group", workdir=str(group_workdir))]),
                patch("qq_onebot_bridge.create_request", side_effect=fake_create_request),
                patch("qq_onebot_bridge.wait_for_response", return_value=response),
            ):
                bridge.handle_event(
                    {
                        "post_type": "message",
                        "message_type": "group",
                        "group_id": 20002,
                        "user_id": 10001,
                        "self_id": 900000001,
                        "message": [
                            {"type": "at", "data": {"qq": "900000001"}},
                            {"type": "text", "data": {"text": "看这张图"}},
                            {"type": "image", "data": {"url": str(source_image), "file": "image.png"}},
                        ],
                    }
                )

            payload = calls[0][1]
            self.assertEqual("qq-group", payload["agent_id"])
            self.assertEqual("read-only", payload["permission_mode"])
            self.assertEqual("qq_group", payload["permission_profile"])
            self.assertIs(True, payload["codex_search_enabled"])
            self.assertEqual(1, len(payload["images"]))
            image_path = str(payload["images"][0])
            self.assertFalse(Path(image_path).is_absolute())
            self.assertIn(QQ_GROUP_ATTACHMENT_DIR_NAME, image_path)
            self.assertIn(image_path, str(payload["prompt"]))
            self.assertNotIn("\n", str(payload["prompt"]))
            self.assertTrue((group_workdir / image_path).exists())

    def test_http_handler_returns_onebot_ok_for_message_event(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        server = ThreadingHTTPServer(("127.0.0.1", 0), bridge.make_handler())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/",
            data=b'{"post_type":"message","message_type":"private","user_id":10001,"message":"hello"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with NO_PROXY_OPENER.open(request, timeout=5) as response:
            body = response.read().decode("utf-8")

        self.assertEqual(200, response.status)
        self.assertIn('"retcode":0', body)

    def test_http_handler_does_not_return_400_for_invalid_payload(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        server = ThreadingHTTPServer(("127.0.0.1", 0), bridge.make_handler())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/",
            data=b"not-json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with NO_PROXY_OPENER.open(request, timeout=5) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            self.fail(f"handler returned unexpected HTTP error: {exc.code}")

        self.assertEqual(200, response.status)
        self.assertIn('"retcode":0', body)

    def test_http_handler_reads_chunked_onebot_event(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        server = ThreadingHTTPServer(("127.0.0.1", 0), bridge.make_handler())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        body = b'{"post_type":"message","message_type":"private","user_id":10001,"message":"chunked hello"}'
        first = body[:23]
        second = body[23:]
        raw_request = (
            f"POST / HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\nTransfer-Encoding: chunked\r\nContent-Type: application/json\r\n\r\n".encode(
                "ascii"
            )
            + f"{len(first):X}\r\n".encode("ascii")
            + first
            + b"\r\n"
            + f"{len(second):X}\r\n".encode("ascii")
            + second
            + b"\r\n0\r\n\r\n"
        )
        with socket.create_connection(("127.0.0.1", server.server_port), timeout=5) as sock:
            sock.sendall(raw_request)
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                if b'{"status":"ok","retcode":0,"data":null}' in b"".join(chunks):
                    break
            response = b"".join(chunks).decode("utf-8", errors="replace")

        self.assertIn("200 OK", response)
        self.assertIn('"retcode":0', response)
        deadline = time.monotonic() + 1.0
        while not bridge.submitted and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(1, len(bridge.submitted))
        self.assertEqual("chunked hello", bridge.submitted[0][1])


if __name__ == "__main__":
    unittest.main()
