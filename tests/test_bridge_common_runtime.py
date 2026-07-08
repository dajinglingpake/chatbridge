from __future__ import annotations

import unittest

from core.bridge_message_format import format_bridge_reply, format_duration_since, prefix_bridge_output
from core.bridge_message_filters import BridgeDuplicateMessageFilter, has_ignored_prefix
from core.bridge_notifier import NoticeResult, broadcast_bridge_notice_by_kind
from core.bridge_typing_runtime import should_refresh_typing_ticket, should_send_typing_keepalive


class BridgeCommonRuntimeTests(unittest.TestCase):
    def test_bridge_reply_format_preserves_existing_header(self) -> None:
        output = prefix_bridge_output("running", "3s", "hello", at="2026-04-23T18:09:46", context_left_percent=20)

        self.assertEqual("running · 3s · ctx 20% · 18:09:46\n\nhello", output)
        self.assertEqual(output, format_bridge_reply(output))

    def test_duration_accepts_utc_z_timestamps(self) -> None:
        self.assertEqual("5s", format_duration_since("2026-07-09T01:36:21Z", ended_at="2026-07-09T01:36:26Z"))
        self.assertEqual("5s", format_duration_since("2026-07-09T01:36:21Z", ended_at="2026-07-09T01:36:26"))

    def test_typing_keepalive_policy(self) -> None:
        self.assertTrue(should_send_typing_keepalive(0, now_seconds=100, keepalive_seconds=5))
        self.assertFalse(should_send_typing_keepalive(98, now_seconds=100, keepalive_seconds=5))
        self.assertTrue(should_send_typing_keepalive(95, now_seconds=100, keepalive_seconds=5))

    def test_typing_ticket_refresh_policy(self) -> None:
        self.assertTrue(should_refresh_typing_ticket("", 100, now_seconds=101, ttl_seconds=10))
        self.assertTrue(should_refresh_typing_ticket("ticket", 0, now_seconds=101, ttl_seconds=10))
        self.assertFalse(should_refresh_typing_ticket("ticket", 95, now_seconds=100, ttl_seconds=10))
        self.assertTrue(should_refresh_typing_ticket("ticket", 90, now_seconds=100, ttl_seconds=10))

    def test_qq_notice_route_does_not_fall_back_to_wechat(self) -> None:
        result = broadcast_bridge_notice_by_kind("task", "任务完成", "done", channel="qq")

        self.assertEqual(NoticeResult(sent_count=0, recipient_count=0, error="unsupported", platform_label="QQ"), result)
        self.assertEqual("QQ系统通知暂不支持", result.summary)

    def test_ignore_prefix_policy(self) -> None:
        self.assertTrue(has_ignored_prefix("/ignore hello", ["/ignore"]))
        self.assertFalse(has_ignored_prefix("hello", ["/ignore"]))

    def test_duplicate_filter_tracks_key_and_command_fingerprint(self) -> None:
        now_value = 100.0

        def now() -> float:
            return now_value

        filter_ = BridgeDuplicateMessageFilter(now=now)
        self.assertFalse(filter_.is_duplicate("key-1", sender_id="sender", text="hello"))
        self.assertTrue(filter_.is_duplicate("key-1", sender_id="sender", text="hello"))
        self.assertFalse(filter_.is_duplicate("key-2", sender_id="sender", text="/status"))
        self.assertTrue(filter_.is_duplicate("key-3", sender_id="sender", text="/status"))


if __name__ == "__main__":
    unittest.main()
