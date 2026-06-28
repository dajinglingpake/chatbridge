from __future__ import annotations

import unittest

from core.bridge_task_delivery import build_terminal_task_delivery_plan
from core.state_models import HubTask


def _task(**overrides: object) -> HubTask:
    payload = {
        "id": "task-001",
        "agent_id": "main",
        "agent_name": "main",
        "backend": "codex",
        "source": "wechat",
        "sender_id": "sender-001",
        "prompt": "hello",
        "status": "succeeded",
        "created_at": "2026-06-26T01:00:00",
        "started_at": "2026-06-26T01:00:01",
        "finished_at": "2026-06-26T01:00:05",
        "output": "done",
        "error": "",
        "session_name": "default",
    }
    payload.update(overrides)
    task = HubTask.from_dict(payload, default_backend="codex")
    assert task is not None
    return task


class BridgeTaskDeliveryTests(unittest.TestCase):
    def test_terminal_plan_keeps_done_header_when_output_matches_progress(self) -> None:
        plan = build_terminal_task_delivery_plan(
            _task(output="final answer"),
            last_progress_text="final answer",
            context_left_percent=42,
        )

        self.assertEqual("succeeded", plan.event)
        self.assertTrue(plan.handled)
        self.assertFalse(plan.failed)
        self.assertTrue(plan.deduped)
        self.assertTrue(plan.reply.startswith("done · "))
        self.assertIn(" · ctx 42% · ", plan.reply.splitlines()[0])
        self.assertNotIn("\n\nfinal answer", plan.reply)

    def test_terminal_plan_formats_canceled_with_retry_hint(self) -> None:
        plan = build_terminal_task_delivery_plan(
            _task(status="canceled", output="", error="Task canceled during execution."),
            session_name="work",
            session_id="session-001",
            backend="codex",
            hint="/retry task-001",
        )

        self.assertEqual("canceled", plan.event)
        self.assertFalse(plan.handled)
        self.assertFalse(plan.failed)
        self.assertIn("codex 任务已取消", plan.reply)
        self.assertIn("会话: work", plan.reply)
        self.assertIn("/retry task-001", plan.reply)
        self.assertEqual("Task canceled during execution.", plan.error_preview)

    def test_terminal_plan_formats_unknown_after_restart_as_restart_interruption(self) -> None:
        plan = build_terminal_task_delivery_plan(
            _task(status="unknown_after_restart", output="", error="lost after restart"),
            session_name="work",
            backend="codex",
        )

        self.assertEqual("restart_interrupted", plan.event)
        self.assertFalse(plan.failed)
        self.assertIn("Hub 已重启，上一轮任务被中断", plan.reply)
        self.assertIn("请重新发送你的问题", plan.reply)
        self.assertNotIn("codex 任务失败", plan.reply)
        self.assertEqual("lost after restart", plan.error_preview)


if __name__ == "__main__":
    unittest.main()
