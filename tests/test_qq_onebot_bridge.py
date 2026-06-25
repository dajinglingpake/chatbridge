from __future__ import annotations

import tempfile
import threading
import unittest
import socket
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.bridge_pending_tasks import BridgePendingReplyTask
from core.state_models import HubTask
import local_ipc
from qq_onebot_bridge import DEFAULT_QQ_AGENT_ID, QQOneBotBridge
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
            SimpleNamespace(backend_id="main", default_backend="codex", hub_task_timeout_seconds=30),
            api_base="http://onebot.local",
        )

    def _download_media_url(self, sender_key: str, url: str, *, filename: str) -> Path:
        target = self.temp_path / f"{sender_key.replace(':', '-')}-{filename}"
        target.write_bytes(f"{url}:{filename}".encode("utf-8"))
        return target

    def _submit_task(self, sender_key: str, prompt: str) -> dict[str, object]:
        self.submitted.append((sender_key, prompt))
        return {"id": "task-qq-001"}

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
        media_patcher = patch("qq_onebot_bridge.ONEBOT_STATE_PATH", self.state_path)
        task_patcher = patch("qq_onebot_bridge.QQ_PENDING_TASKS_PATH", self.pending_tasks_path)
        media_patcher.start()
        task_patcher.start()
        self.addCleanup(media_patcher.stop)
        self.addCleanup(task_patcher.stop)

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
        self.assertIn("图片: image.png", prompt)
        self.assertIn("qq-private-10001-image.png", prompt)
        self.assertNotIn("qq:private:10001", bridge.pending_media_context)

    def test_group_text_with_file_submits_prompt_with_attachment(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 20002,
                "user_id": 10001,
                "self_id": 2493227263,
                "message": [
                    {"type": "at", "data": {"qq": "2493227263"}},
                    {"type": "text", "data": {"text": "总结这个文件"}},
                    {"type": "file", "data": {"url": "http://media.local/file", "file": "report.pdf"}},
                ],
            }
        )

        sender_key, prompt = bridge.submitted[0]
        self.assertEqual("qq:group:20002:10001", sender_key)
        self.assertTrue(prompt.startswith("总结这个文件"))
        self.assertIn("文件: report.pdf", prompt)

    def test_group_message_without_at_self_is_ignored(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 20002,
                "user_id": 10001,
                "self_id": 2493227263,
                "message": [
                    {"type": "text", "data": {"text": "不要回复这条"}},
                ],
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
                "self_id": 2493227263,
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

        self.assertEqual(
            [
                ("send_private_msg", {"user_id": 10001, "message": "hello"}),
                ("send_group_msg", {"group_id": 20002, "message": "world"}),
            ],
            bridge.api_calls,
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
        self.assertEqual(1, len(bridge.submitted))
        self.assertEqual("chunked hello", bridge.submitted[0][1])


if __name__ == "__main__":
    unittest.main()
