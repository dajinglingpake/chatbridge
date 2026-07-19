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
        self.assertIn("itemsView: 'summary'", expression)
        self.assertIn("item?.clientId", expression)
        self.assertIn("reconciled: true", expression)
        self.assertIn('"type": "localImage"', expression)
        self.assertIn("chatbridge:message-001", expression)
        self.assertNotIn("process.kill", expression)

    def test_goal_expression_uses_native_goal_protocol_and_interrupts_pause(self) -> None:
        expression = control._goal_expression(
            "thread-001",
            "pause",
            "继续完成目标",
            timeout_seconds=5,
        )

        self.assertIn("thread/resume", expression)
        self.assertIn("thread/goal/get", expression)
        self.assertIn("thread/goal/set", expression)
        self.assertIn("status = action === 'pause' ? 'paused' : 'active'", expression)
        self.assertIn("turn/interrupt", expression)
        self.assertIn("\\u7ee7\\u7eed\\u5b8c\\u6210\\u76ee\\u6807", expression)

    def test_goal_expression_uses_native_clear_for_delete(self) -> None:
        expression = control._goal_expression("thread-001", "delete", "", timeout_seconds=5)

        self.assertIn("thread/goal/clear", expression)
        self.assertIn("status: 'cleared'", expression)

    def test_goal_get_expression_reads_current_desktop_goal(self) -> None:
        expression = control._goal_get_expression("thread-001", timeout_seconds=5)

        self.assertIn("thread/goal/get", expression)
        self.assertIn("thread-001", expression)
        self.assertIn("goal: response?.result?.goal || null", expression)

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
                        "reconciled": True,
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
        self.assertTrue(result.reconciled)
        self.assertTrue(result.client_user_message_id.startswith("chatbridge:"))
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

    def test_run_desktop_action_preserves_distinct_window_errors(self) -> None:
        with (
            patch.object(
                control,
                "_codex_desktop_websocket_urls",
                return_value=["ws://127.0.0.1/devtools/page/1", "ws://127.0.0.1/devtools/page/2"],
            ),
            patch.object(
                control,
                "_evaluate_cdp",
                side_effect=[
                    json.dumps({"ok": False, "error": "active turn cannot be steered"}),
                    RuntimeError("connection closed"),
                ],
            ),
        ):
            with self.assertRaises(control.CodexDesktopControlError) as raised:
                control._run_desktop_action("expression", timeout_seconds=5, failure_message="send failed")

        self.assertIn("active turn cannot be steered", str(raised.exception))
        self.assertIn("connection closed", str(raised.exception))

    def test_control_codex_desktop_thread_goal_returns_goal_state(self) -> None:
        with patch.object(
            control,
            "_run_desktop_action",
            return_value={
                "ok": True,
                "action": "pause",
                "goal": {"objective": "继续完成目标", "status": "paused"},
                "interruptedTurnId": "turn-001",
            },
        ) as run_action:
            result = control.control_codex_desktop_thread_goal(
                " thread-001 ",
                "pause",
                objective=" 继续完成目标 ",
                timeout_seconds=7,
            )

        self.assertEqual("pause", result.action)
        self.assertEqual("paused", result.goal["status"])
        self.assertEqual("turn-001", result.interrupted_turn_id)
        self.assertIn("thread/goal/set", run_action.call_args.args[0])

    def test_control_codex_desktop_thread_goal_rejects_empty_edit(self) -> None:
        with self.assertRaisesRegex(control.CodexDesktopControlError, "目标内容不能为空"):
            control.control_codex_desktop_thread_goal("thread-001", "edit", objective="   ")

    def test_get_codex_desktop_thread_goal_returns_goal_or_none(self) -> None:
        with patch.object(
            control,
            "_run_desktop_action",
            side_effect=[
                {"ok": True, "goal": {"objective": "继续目标", "status": "active"}},
                {"ok": True, "goal": None},
            ],
        ) as run_action:
            active = control.get_codex_desktop_thread_goal(" thread-001 ", timeout_seconds=7)
            cleared = control.get_codex_desktop_thread_goal("thread-001", timeout_seconds=7)

        self.assertEqual("active", active["status"])
        self.assertIsNone(cleared)
        self.assertIn("thread/goal/get", run_action.call_args_list[0].args[0])


if __name__ == "__main__":
    unittest.main()
