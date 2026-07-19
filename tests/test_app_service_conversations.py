from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bridge_config import BridgeConfig
from core import app_service
from core.state_models import RuntimeSnapshot
from core.bridge_notifier import NoticeResult


def _runtime_snapshot(*, bridge_running: bool) -> RuntimeSnapshot:
    return RuntimeSnapshot(
        hub_running=False,
        bridge_running=bridge_running,
        qq_bridge_running=False,
        onebot_runtime_running=False,
        hub_pid=None,
        bridge_pid=202 if bridge_running else None,
        qq_bridge_pid=None,
        onebot_runtime_pid=None,
        codex_processes=[],
        log_dir=".runtime/logs",
    )


def _bridge_config(*, default_backend: str = "codex") -> BridgeConfig:
    return BridgeConfig(default_backend=default_backend)


def _notice_result(summary_error: str = "") -> NoticeResult:
    return NoticeResult(sent_count=0, recipient_count=0, error=summary_error)


class AppServiceConversationTests(unittest.TestCase):
    def test_switch_weixin_session_backend_updates_current_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conversations_path = Path(temp_dir) / "weixin_conversations.json"
            conversations_path.write_text(
                json.dumps(
                    {
                        "sender-a": {
                            "current_session": "focus",
                            "sessions": {
                                "focus": {"backend": "codex"},
                            },
                        }
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            with (
                patch.object(app_service, "BRIDGE_CONVERSATIONS_PATH", conversations_path),
                patch.object(app_service.BridgeConfig, "load", return_value=_bridge_config(default_backend="codex")),
                patch.object(app_service, "get_runtime_snapshot", return_value=_runtime_snapshot(bridge_running=False)),
            patch.object(app_service, "broadcast_bridge_notice_by_kind", return_value=_notice_result()),
            ):
                result = app_service.switch_weixin_session_backend("sender-a", "claude")

            self.assertTrue(result.ok)
            payload = json.loads(conversations_path.read_text(encoding="utf-8"))
            self.assertEqual("claude", payload["sender-a"]["sessions"]["focus"]["backend"])

    def test_reset_weixin_conversation_removes_sender_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conversations_path = Path(temp_dir) / "weixin_conversations.json"
            conversations_path.write_text(
                json.dumps(
                    {
                        "sender-a": {
                            "current_session": "default",
                            "sessions": {
                                "default": {"backend": "codex"},
                            },
                        },
                        "sender-b": {
                            "current_session": "default",
                            "sessions": {
                                "default": {"backend": "claude"},
                            },
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            with (
                patch.object(app_service, "BRIDGE_CONVERSATIONS_PATH", conversations_path),
                patch.object(app_service.BridgeConfig, "load", return_value=_bridge_config(default_backend="codex")),
                patch.object(app_service, "get_runtime_snapshot", return_value=_runtime_snapshot(bridge_running=False)),
            patch.object(app_service, "broadcast_bridge_notice_by_kind", return_value=_notice_result()),
            ):
                result = app_service.reset_weixin_conversation("sender-a")

            self.assertTrue(result.ok)
            payload = json.loads(conversations_path.read_text(encoding="utf-8"))
            self.assertNotIn("sender-a", payload)
            self.assertIn("sender-b", payload)

    def test_switch_sender_current_session_updates_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conversations_path = Path(temp_dir) / "weixin_conversations.json"
            conversations_path.write_text(
                json.dumps(
                    {
                        "qq:private:10001": {
                            "current_session": "default",
                            "sessions": {
                                "default": {"backend": "codex"},
                            },
                        }
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            with (
                patch.object(app_service, "BRIDGE_CONVERSATIONS_PATH", conversations_path),
                patch.object(app_service.BridgeConfig, "load", return_value=_bridge_config(default_backend="codex")),
            ):
                result = app_service.switch_sender_current_session("qq:private:10001", "ui-fix")

            self.assertTrue(result.ok)
            payload = json.loads(conversations_path.read_text(encoding="utf-8"))
            binding = payload["qq:private:10001"]
            self.assertEqual("ui-fix", binding["current_session"])
            self.assertEqual("ui-fix", binding["last_regular_session"])
            self.assertIn("ui-fix", binding["sessions"])

    def test_cancel_hub_task_sends_cancel_request(self) -> None:
        requests: list[tuple[str, dict[str, object]]] = []

        def create_request(action: str, payload: dict[str, object]) -> str:
            requests.append((action, payload))
            return "request-cancel"

        response = SimpleNamespace(
            ok=True,
            error="",
            payload={
                "task": {
                    "id": "task-001",
                    "agent_id": "main",
                    "status": "canceled",
                    "session_name": "focus",
                }
            },
        )

        with (
            patch.object(app_service, "create_request", side_effect=create_request),
            patch.object(app_service, "wait_for_response", return_value=response) as wait_for_response,
        ):
            result = app_service.cancel_hub_task(" task-001 ")

        self.assertTrue(result.ok)
        self.assertEqual([("cancel_task", {"task_id": "task-001"})], requests)
        wait_for_response.assert_called_once_with("request-cancel", timeout_seconds=5)
        self.assertIn("task-001", result.message)
        self.assertIn("focus", result.message)

    def test_interrupt_codex_thread_uses_desktop_bridge(self) -> None:
        with patch.object(app_service, "interrupt_codex_desktop_thread", return_value="turn-001") as interrupt:
            result = app_service.interrupt_codex_thread(" thread-001 ", timeout_seconds=9)

        self.assertTrue(result.ok)
        self.assertIn("thread-001", result.message)
        self.assertIn("turn-001", result.message)
        interrupt.assert_called_once_with("thread-001", timeout_seconds=9)

    def test_send_codex_thread_message_uses_desktop_bridge(self) -> None:
        bridge_result = SimpleNamespace(mode="steer", turn_id="turn-002")
        with patch.object(app_service, "send_codex_desktop_thread_message", return_value=bridge_result) as send:
            result = app_service.send_codex_thread_message(
                " thread-001 ",
                " continue ",
                images=["C:/tmp/shot.png"],
                timeout_seconds=9,
            )

        self.assertTrue(result.ok)
        self.assertIn("已追加到运行中的轮次", result.message)
        self.assertIn("turn-002", result.message)
        send.assert_called_once_with(
            "thread-001",
            "continue",
            images=["C:/tmp/shot.png"],
            timeout_seconds=9,
        )

    def test_send_codex_thread_message_reports_desktop_bridge_failure(self) -> None:
        with patch.object(
            app_service,
            "send_codex_desktop_thread_message",
            side_effect=app_service.CodexDesktopControlError("desktop unavailable"),
        ):
            result = app_service.send_codex_thread_message("thread-001", "continue")

        self.assertFalse(result.ok)
        self.assertIn("desktop unavailable", result.message)

    def test_control_codex_thread_goal_uses_desktop_bridge(self) -> None:
        bridge_result = SimpleNamespace(
            action="pause",
            goal={"objective": "继续目标", "status": "paused"},
            interrupted_turn_id="turn-003",
        )
        with patch.object(app_service, "control_codex_desktop_thread_goal", return_value=bridge_result) as control_goal:
            result = app_service.control_codex_thread_goal(
                " thread-001 ",
                "pause",
                objective=" 继续目标 ",
                timeout_seconds=9,
            )

        self.assertTrue(result.ok)
        self.assertEqual("paused", result.payload["goal"]["status"])
        self.assertEqual("turn-003", result.payload["interrupted_turn_id"])
        control_goal.assert_called_once_with(
            "thread-001",
            "pause",
            objective=" 继续目标 ",
            timeout_seconds=9,
        )

    def test_control_codex_thread_goal_reports_desktop_bridge_failure(self) -> None:
        with patch.object(
            app_service,
            "control_codex_desktop_thread_goal",
            side_effect=app_service.CodexDesktopControlError("desktop unavailable"),
        ):
            result = app_service.control_codex_thread_goal("thread-001", "delete")

        self.assertFalse(result.ok)
        self.assertIn("desktop unavailable", result.message)

    def test_submit_hub_task_includes_images(self) -> None:
        requests: list[tuple[str, dict[str, object]]] = []

        def create_request(action: str, payload: dict[str, object]) -> str:
            requests.append((action, payload))
            return "request-submit"

        response = SimpleNamespace(
            ok=True,
            error="",
            payload={
                "task": {
                    "id": "task-image-001",
                    "agent_id": "main",
                    "status": "queued",
                    "session_name": "focus",
                }
            },
        )

        with (
            patch.object(app_service, "create_request", side_effect=create_request),
            patch.object(app_service, "wait_for_response", return_value=response),
            patch.object(app_service, "broadcast_bridge_notice_by_kind", return_value=_notice_result()),
        ):
            result = app_service.submit_hub_task(
                agent_id="main",
                prompt="inspect image",
                session_name="focus",
                images=[" C:/tmp/a.png ", "", "C:/tmp/b.jpg"],
            )

        self.assertTrue(result.ok)
        self.assertEqual("submit_task", requests[0][0])
        self.assertEqual(["C:/tmp/a.png", "C:/tmp/b.jpg"], requests[0][1]["images"])


if __name__ == "__main__":
    unittest.main()
