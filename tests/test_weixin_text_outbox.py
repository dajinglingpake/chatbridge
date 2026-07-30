from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.weixin_text_outbox import enqueue_text_message, pop_text_messages, requeue_text_message


class WeixinTextOutboxTests(unittest.TestCase):
    def test_requeue_text_message_sets_retry_not_before(self) -> None:
        payload = {
            "id": "msg-1",
            "to_user_id": "sender-test",
            "context_token": "ctx",
            "text": "hello",
            "attempt": 0,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            outbox_path = Path(temp_dir) / "weixin_text_outbox.jsonl"
            with patch("core.weixin_text_outbox.OUTBOX_PATH", outbox_path):
                with patch("core.weixin_text_outbox.time.time", return_value=100):
                    requeue_text_message(payload)
                with patch("core.weixin_text_outbox.time.time", return_value=102):
                    popped = pop_text_messages(limit=10)
        self.assertEqual(1, len(popped))
        self.assertEqual(1, int(popped[0]["attempt"]))
        self.assertEqual(102, int(popped[0]["retry_not_before"]))

    def test_pop_text_messages_skips_future_retry_items(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            outbox_path = Path(temp_dir) / "weixin_text_outbox.jsonl"
            with patch("core.weixin_text_outbox.OUTBOX_PATH", outbox_path):
                with patch("core.weixin_text_outbox.time.time", return_value=100):
                    enqueue_text_message(to_user_id="sender-test", context_token="ctx", text="hello")
                queued = pop_text_messages(limit=10)
                self.assertEqual(1, len(queued))
                with patch("core.weixin_text_outbox.time.time", return_value=100):
                    requeue_text_message(queued[0])
                    skipped = pop_text_messages(limit=10)
                self.assertEqual([], skipped)
                with patch("core.weixin_text_outbox.time.time", return_value=103):
                    retried = pop_text_messages(limit=10)
        self.assertEqual(1, len(retried))
        self.assertEqual("hello", retried[0]["text"])

    def test_enqueue_text_message_records_account_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            outbox_path = Path(temp_dir) / "weixin_text_outbox.jsonl"
            with patch("core.weixin_text_outbox.OUTBOX_PATH", outbox_path):
                enqueue_text_message(
                    to_user_id="sender-test",
                    context_token="ctx",
                    text="hello",
                    account_id="bot-a",
                    account_file="/tmp/bot-a.json",
                    progress_task_id="task-progress-001",
                    progress_text="working",
                )
                queued = pop_text_messages(limit=10)

        self.assertEqual(1, len(queued))
        self.assertEqual("bot-a", queued[0]["account_id"])
        self.assertEqual("/tmp/bot-a.json", queued[0]["account_file"])
        self.assertEqual("task-progress-001", queued[0]["progress_task_id"])
        self.assertEqual("working", queued[0]["progress_text"])


    def test_enqueue_text_message_preserves_terminal_messages_when_progress_is_superseded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            outbox_path = Path(temp_dir) / "weixin_text_outbox.jsonl"
            with patch("core.weixin_text_outbox.OUTBOX_PATH", outbox_path):
                enqueue_text_message(
                    to_user_id="sender-test",
                    context_token="ctx",
                    text="running · 3s · 19:10:30\n\nold progress",
                    supersede_pending_progress=True,
                )
                enqueue_text_message(to_user_id="other-sender", context_token="ctx", text="done · 26s · ctx 47% · 19:10:35")
                enqueue_text_message(to_user_id="sender-test", context_token="ctx", text="done · 4s · 19:10:31\n\nfinal answer")
                enqueue_text_message(
                    to_user_id="sender-test",
                    context_token="ctx",
                    text="running · 1s · 19:11:00\n\nnew progress",
                    supersede_pending_progress=True,
                )
                queued = pop_text_messages(limit=10)

        self.assertEqual(
            [
                "done · 26s · ctx 47% · 19:10:35",
                "done · 4s · 19:10:31\n\nfinal answer",
                "running · 1s · 19:11:00\n\nnew progress",
            ],
            [str(item["text"]) for item in queued],
        )
        self.assertEqual(["other-sender", "sender-test", "sender-test"], [str(item["to_user_id"]) for item in queued])

if __name__ == "__main__":
    unittest.main()
