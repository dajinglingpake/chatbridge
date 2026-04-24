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


if __name__ == "__main__":
    unittest.main()
