from __future__ import annotations

import threading
import time
import unittest
from dataclasses import dataclass, field
from typing import Any

from core.bridge_interrupt_runtime import BridgeInterruptRuntime, render_interrupt_prompt
from core.bridge_runtime import IncomingBridgeMessage


@dataclass
class FakePendingTask:
    task_id: str
    interrupt_base_prompt: str = ""
    interrupt_messages: list[str] = field(default_factory=list)


class BridgeInterruptRuntimeTests(unittest.TestCase):
    def _message(self, text: str, *, source: str = "qq", context_token: str = "ctx-001") -> IncomingBridgeMessage:
        return IncomingBridgeMessage(
            sender_id="sender-a",
            text=text,
            reply_target={"id": "sender-a"},
            source=source,
            session_name="session-a",
            attachments=[],
            attachment_errors=[],
            message_type="private",
            context_token=context_token,
        )

    def test_render_interrupt_prompt_stays_flat(self) -> None:
        prompt = render_interrupt_prompt("原始问题", ["补充一", "补充二"])

        self.assertIn("上一轮用户问题：\n原始问题", prompt)
        self.assertIn("1. 补充一", prompt)
        self.assertIn("2. 补充二", prompt)
        self.assertEqual(1, prompt.count("用户在你处理上一轮问题时打断并补充："))

    def test_interrupt_cancels_once_and_delays_flat_resubmit(self) -> None:
        initial_task = FakePendingTask("task-001", interrupt_base_prompt="原始问题")
        active_task: FakePendingTask | None = initial_task
        cancelled: list[str] = []
        replies: list[str] = []
        saved: list[list[str]] = []
        submitted: list[tuple[IncomingBridgeMessage, Any, str, bool]] = []
        submitted_event = threading.Event()

        def find_active_task(_sender_id: str) -> FakePendingTask | None:
            return active_task

        def cancel_task(task_id: str) -> None:
            nonlocal active_task
            cancelled.append(task_id)
            active_task = None

        def submit_delayed(message: IncomingBridgeMessage, session: Any, prompt: str, passthrough: bool) -> None:
            submitted.append((message, session, prompt, passthrough))
            submitted_event.set()

        runtime = BridgeInterruptRuntime(
            find_active_task=find_active_task,
            cancel_task=cancel_task,
            submit_delayed=submit_delayed,
            send_reply=lambda _target, text: replies.append(text),
            save_pending_tasks=lambda: saved.append(list(initial_task.interrupt_messages)),
            delay_seconds=0.08,
        )

        self.assertTrue(runtime.intercept(self._message("补充一"), {"session_name": "session-a"}, "补充一"))
        time.sleep(0.04)
        self.assertTrue(runtime.intercept(self._message("补充二", context_token="ctx-002"), {"session_name": "session-b"}, "补充二", passthrough=True))

        self.assertTrue(submitted_event.wait(1.0))
        self.assertEqual(["task-001"], cancelled)
        self.assertEqual(1, len(replies))
        self.assertEqual([["补充一"]], saved)
        self.assertEqual(["补充一"], initial_task.interrupt_messages)
        self.assertEqual(1, len(submitted))

        delayed_message, session, prompt, passthrough = submitted[0]
        self.assertEqual("qq", delayed_message.source)
        self.assertEqual("session-b", delayed_message.session_name)
        self.assertEqual("private", delayed_message.message_type)
        self.assertEqual("ctx-002", delayed_message.context_token)
        self.assertEqual({"session_name": "session-b"}, session)
        self.assertTrue(passthrough)
        self.assertEqual({"interrupt_base_prompt": "原始问题", "interrupt_messages": ["补充一", "补充二"], "interrupted": True}, delayed_message.metadata)
        self.assertIn("上一轮用户问题：\n原始问题", prompt)
        self.assertIn("1. 补充一", prompt)
        self.assertIn("2. 补充二", prompt)

    def test_no_active_task_does_not_intercept(self) -> None:
        runtime = BridgeInterruptRuntime(
            find_active_task=lambda _sender_id: None,
            cancel_task=lambda _task_id: None,
            submit_delayed=lambda _message, _session, _prompt, _passthrough: None,
            send_reply=lambda _target, _text: None,
            save_pending_tasks=lambda: None,
            delay_seconds=0.08,
        )

        self.assertFalse(runtime.intercept(self._message("普通问题", source="wechat"), {"session_name": "session-a"}, "普通问题"))


if __name__ == "__main__":
    unittest.main()
