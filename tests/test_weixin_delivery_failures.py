from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.weixin_delivery_failures import pop_failed_delivery, record_failed_delivery
from core.weixin_text_outbox import MAX_RETRY_ATTEMPTS, pop_text_messages
from weixin_hub_bridge import (
    WeixinBridge,
    _is_ephemeral_delivery_text,
    _is_permanent_delivery_error,
    _is_stale_ephemeral_delivery,
    _is_stale_notice_delivery,
)


class WeixinDeliveryFailuresTests(unittest.TestCase):
    def test_record_failed_delivery_accumulates_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            failure_path = Path(temp_dir) / "weixin_failed_deliveries.json"
            with patch("core.weixin_delivery_failures.FAILED_DELIVERIES_PATH", failure_path):
                record_failed_delivery(
                    to_user_id="sender-test",
                    context_token="ctx",
                    text_preview="done · 10s",
                    attempts=6,
                    error="ret=-2",
                )
                record_failed_delivery(
                    to_user_id="sender-test",
                    context_token="ctx",
                    text_preview="done · 11s",
                    attempts=6,
                    error="ret=-2",
                )
                payload = pop_failed_delivery("sender-test")
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(2, payload["count"])
        self.assertEqual("done · 11s", payload["text_preview"])

    def test_permanent_delivery_error_matches_invalid_session_failures(self) -> None:
        self.assertFalse(_is_permanent_delivery_error("sendmessage returned ret=-2: {'ret': -2}"))
        self.assertTrue(_is_permanent_delivery_error("errcode=-14 errmsg=session timeout"))
        self.assertFalse(_is_permanent_delivery_error("timed out"))

    def test_ret_minus_two_is_requeued_before_retry_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            outbox_path = Path(temp_dir) / "weixin_text_outbox.jsonl"
            failure_path = Path(temp_dir) / "weixin_failed_deliveries.json"
            message = {
                "id": "msg-1",
                "to_user_id": "sender-test",
                "context_token": "ctx",
                "text": "hello",
                "attempt": 0,
            }

            with (
                patch("core.weixin_text_outbox.OUTBOX_PATH", outbox_path),
                patch("core.weixin_delivery_failures.FAILED_DELIVERIES_PATH", failure_path),
                patch("core.weixin_text_outbox.time.time", return_value=100),
            ):
                WeixinBridge._handle_async_send_failure(None, message, RuntimeError("sendmessage returned ret=-2: {'ret': -2}"))  # type: ignore[arg-type]

            with (
                patch("core.weixin_text_outbox.OUTBOX_PATH", outbox_path),
                patch("core.weixin_text_outbox.time.time", return_value=102),
            ):
                queued = pop_text_messages()

            self.assertEqual(1, len(queued))
            self.assertEqual(1, int(queued[0]["attempt"]))
            self.assertFalse(failure_path.exists())

    def test_running_progress_is_not_recorded_as_failed_delivery_after_retries(self) -> None:
        self.assertTrue(_is_ephemeral_delivery_text("running · 10s · 10:00:00\n\nworking"))
        self.assertFalse(_is_ephemeral_delivery_text("done · 10s · 10:00:00\n\nfinished"))
        with tempfile.TemporaryDirectory() as temp_dir:
            outbox_path = Path(temp_dir) / "weixin_text_outbox.jsonl"
            failure_path = Path(temp_dir) / "weixin_failed_deliveries.json"
            message = {
                "id": "msg-1",
                "to_user_id": "sender-test",
                "context_token": "ctx",
                "text": "running · 10s · 10:00:00\n\nworking",
                "attempt": MAX_RETRY_ATTEMPTS,
            }

            with (
                patch("core.weixin_text_outbox.OUTBOX_PATH", outbox_path),
                patch("core.weixin_delivery_failures.FAILED_DELIVERIES_PATH", failure_path),
            ):
                WeixinBridge._handle_async_send_failure(None, message, RuntimeError("sendmessage returned ret=-2: {'ret': -2}"))  # type: ignore[arg-type]

            self.assertFalse(outbox_path.exists())
            self.assertFalse(failure_path.exists())


    def test_running_progress_is_not_requeued_after_transient_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            outbox_path = Path(temp_dir) / "weixin_text_outbox.jsonl"
            failure_path = Path(temp_dir) / "weixin_failed_deliveries.json"
            message = {
                "id": "msg-1",
                "to_user_id": "sender-test",
                "context_token": "ctx",
                "text": "running · 10s · 10:00:00\n\nworking",
                "attempt": 0,
            }

            with (
                patch("core.weixin_text_outbox.OUTBOX_PATH", outbox_path),
                patch("core.weixin_delivery_failures.FAILED_DELIVERIES_PATH", failure_path),
            ):
                WeixinBridge._handle_async_send_failure(None, message, RuntimeError("sendmessage returned ret=-2: {'ret': -2}"))  # type: ignore[arg-type]

            self.assertFalse(outbox_path.exists())
            self.assertFalse(failure_path.exists())

    def test_notice_is_not_requeued_after_transient_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            outbox_path = Path(temp_dir) / "weixin_text_outbox.jsonl"
            failure_path = Path(temp_dir) / "weixin_failed_deliveries.json"
            message = {
                "id": "msg-1",
                "to_user_id": "sender-test",
                "context_token": "ctx",
                "text": "notice · - · 10:00:00\n\nBridge started",
                "source": "notice",
                "attempt": 0,
            }

            with (
                patch("core.weixin_text_outbox.OUTBOX_PATH", outbox_path),
                patch("core.weixin_delivery_failures.FAILED_DELIVERIES_PATH", failure_path),
            ):
                WeixinBridge._handle_async_send_failure(None, message, RuntimeError("sendmessage returned ret=-2: {'ret': -2}"))  # type: ignore[arg-type]

            self.assertFalse(outbox_path.exists())
            self.assertFalse(failure_path.exists())

    def test_empty_done_is_requeued_after_transient_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            outbox_path = Path(temp_dir) / "weixin_text_outbox.jsonl"
            failure_path = Path(temp_dir) / "weixin_failed_deliveries.json"
            message = {
                "id": "msg-1",
                "to_user_id": "sender-test",
                "context_token": "ctx",
                "text": "done · 26s · ctx 47% · 19:10:35",
                "source": "bridge",
                "attempt": 0,
            }

            with (
                patch("core.weixin_text_outbox.OUTBOX_PATH", outbox_path),
                patch("core.weixin_delivery_failures.FAILED_DELIVERIES_PATH", failure_path),
                patch("core.weixin_text_outbox.time.time", return_value=100),
            ):
                WeixinBridge._handle_async_send_failure(None, message, RuntimeError("sendmessage returned ret=-2: {'ret': -2}"))  # type: ignore[arg-type]

            with (
                patch("core.weixin_text_outbox.OUTBOX_PATH", outbox_path),
                patch("core.weixin_text_outbox.time.time", return_value=102),
            ):
                queued = pop_text_messages(limit=10)

            self.assertEqual(1, len(queued))
            self.assertEqual(1, int(queued[0]["attempt"]))
            self.assertFalse(failure_path.exists())

    def test_stale_ephemeral_delivery_only_matches_old_progress(self) -> None:
        self.assertTrue(_is_stale_ephemeral_delivery("running · 10s · 10:00:00\n\nworking", 31_000))
        self.assertFalse(_is_stale_ephemeral_delivery("running · 10s · 10:00:00\n\nworking", 30_000))
        self.assertFalse(_is_stale_ephemeral_delivery("done · 10s · 10:00:00\n\nfinished", 31_000))

    def test_stale_notice_delivery_only_matches_old_notice(self) -> None:
        self.assertTrue(_is_stale_notice_delivery({"source": "notice"}, 31_000))
        self.assertFalse(_is_stale_notice_delivery({"source": "notice"}, 30_000))
        self.assertFalse(_is_stale_notice_delivery({"source": "bridge"}, 31_000))

if __name__ == "__main__":
    unittest.main()
