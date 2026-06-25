from __future__ import annotations

import unittest

from core.bridge_message_control import extract_progress_delta, should_send_progress_delta


class BridgeMessageControlTests(unittest.TestCase):
    def test_extract_progress_delta_strips_leading_continuation_punctuation(self) -> None:
        previous = "浮浮酱再做一次只读核验：看最新 QQ 桥日志、当前任务状态和测试结果，确认没有重复 running、ctx 丢失或桥进程异常喵～主人"
        current = previous + "，浮浮酱又检查了一次喵～\n\n当前状态正常。"

        self.assertEqual("浮浮酱又检查了一次喵～\n\n当前状态正常。", extract_progress_delta(previous, current))

    def test_extract_progress_delta_keeps_plain_added_text(self) -> None:
        self.assertEqual("第二段内容完成", extract_progress_delta("第一段内容完成\n", "第一段内容完成\n第二段内容完成"))

    def test_should_send_progress_delta_rejects_only_continuation_mark(self) -> None:
        self.assertFalse(should_send_progress_delta("，"))


if __name__ == "__main__":
    unittest.main()
