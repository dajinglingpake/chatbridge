from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from core import codex_desktop_control as control


class CodexDesktopControlTests(unittest.TestCase):
    def test_remote_debugging_port_accepts_supported_argument_forms(self) -> None:
        self.assertEqual(9335, control._remote_debugging_port(["ChatGPT.exe", "--remote-debugging-port=9335"]))
        self.assertEqual(9444, control._remote_debugging_port(["ChatGPT.exe", "--remote-debugging-port", "9444"]))
        self.assertIsNone(control._remote_debugging_port(["ChatGPT.exe", "--remote-debugging-port=0"]))

    def test_interrupt_expression_queries_latest_turn_before_interrupting(self) -> None:
        expression = control._interrupt_expression("thread-001", timeout_seconds=5)

        self.assertIn("thread/turns/list", expression)
        self.assertIn("turn/interrupt", expression)
        self.assertIn("thread-001", expression)
        self.assertNotIn("process.kill", expression)

    def test_message_expression_steers_running_turn_or_starts_idle_thread(self) -> None:
        expression = control._message_expression(
            "thread-001",
            "继续检查",
            ["C:/tmp/shot.png"],
            client_user_message_id="chatbridge:message-001",
            timeout_seconds=5,
        )

        self.assertIn("thread/turns/list", expression)
        self.assertIn("turn/steer", expression)
        self.assertIn("expectedTurnId: activeTurnId", expression)
        self.assertIn("refreshedActiveTurnId", expression)
        self.assertIn("turn/start", expression)
        self.assertIn('"type": "localImage"', expression)
        self.assertIn("chatbridge:message-001", expression)
        self.assertNotIn("process.kill", expression)

    def test_interrupt_codex_desktop_thread_returns_the_interrupted_turn_id(self) -> None:
        with (
            patch.object(control, "_codex_desktop_websocket_urls", return_value=["ws://127.0.0.1/devtools/page/1"]),
            patch.object(
                control,
                "_evaluate_cdp",
                return_value=json.dumps(
                    {
                        "ok": True,
                        "threadId": "thread-001",
                        "turnId": "turn-001",
                    }
                ),
            ) as evaluate,
        ):
            turn_id = control.interrupt_codex_desktop_thread(" thread-001 ", timeout_seconds=7)

        self.assertEqual("turn-001", turn_id)
        expression = evaluate.call_args.args[1]
        self.assertIn("thread-001", expression)
        self.assertEqual(7, evaluate.call_args.kwargs["timeout_seconds"])

    def test_interrupt_codex_desktop_thread_refuses_when_no_safe_window_exists(self) -> None:
        with patch.object(control, "_codex_desktop_websocket_urls", return_value=[]):
            with self.assertRaisesRegex(control.CodexDesktopControlError, "未发现可安全控制"):
                control.interrupt_codex_desktop_thread("thread-001")

    def test_interrupt_codex_desktop_thread_wraps_transport_failures(self) -> None:
        with (
            patch.object(control, "_codex_desktop_websocket_urls", return_value=["ws://127.0.0.1/devtools/page/1"]),
            patch.object(control, "_evaluate_cdp", side_effect=RuntimeError("connection closed")),
        ):
            with self.assertRaisesRegex(control.CodexDesktopControlError, "connection closed"):
                control.interrupt_codex_desktop_thread("thread-001")

    def test_send_codex_desktop_thread_message_returns_steer_result(self) -> None:
        with (
            patch.object(control, "_codex_desktop_websocket_urls", return_value=["ws://127.0.0.1/devtools/page/1"]),
            patch.object(
                control,
                "_evaluate_cdp",
                return_value=json.dumps(
                    {
                        "ok": True,
                        "threadId": "thread-001",
                        "turnId": "turn-001",
                        "mode": "steer",
                    }
                ),
            ) as evaluate,
        ):
            result = control.send_codex_desktop_thread_message(
                " thread-001 ",
                " continue ",
                images=[" C:/tmp/a.png ", "C:/tmp/a.png", ""],
                timeout_seconds=7,
            )

        self.assertEqual("steer", result.mode)
        self.assertEqual("turn-001", result.turn_id)
        expression = evaluate.call_args.args[1]
        self.assertEqual(1, expression.count('"path": "C:/tmp/a.png"'))
        self.assertIn('"text": "continue"', expression)

    def test_send_codex_desktop_thread_message_returns_start_result(self) -> None:
        with (
            patch.object(control, "_codex_desktop_websocket_urls", return_value=["ws://127.0.0.1/devtools/page/1"]),
            patch.object(
                control,
                "_evaluate_cdp",
                return_value=json.dumps(
                    {
                        "ok": True,
                        "threadId": "thread-001",
                        "turnId": "turn-002",
                        "mode": "start",
                    }
                ),
            ),
        ):
            result = control.send_codex_desktop_thread_message("thread-001", "continue")

        self.assertEqual("start", result.mode)
        self.assertEqual("turn-002", result.turn_id)

    def test_send_codex_desktop_thread_message_refuses_empty_text(self) -> None:
        with self.assertRaisesRegex(control.CodexDesktopControlError, "消息内容不能为空"):
            control.send_codex_desktop_thread_message("thread-001", "   ")


if __name__ == "__main__":
    unittest.main()
