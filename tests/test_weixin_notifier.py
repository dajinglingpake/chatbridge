from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge_config import BridgeConfig
from core import weixin_notifier


class WeixinNotifierTests(unittest.TestCase):
    def test_load_recipient_ids_filters_blank_sender_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "weixin_conversations.json"
            path.write_text(
                json.dumps(
                    {
                        "sender-a": {"current_session": "default"},
                        "": {"current_session": "default"},
                        " sender-b ": {"current_session": "focus"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch.object(weixin_notifier, "BRIDGE_CONVERSATIONS_PATH", path):
                recipients = weixin_notifier._load_recipient_ids()

            self.assertEqual(["sender-a", "sender-b"], recipients)

    def test_load_recipients_uses_saved_context_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            conversations_path = temp_path / "weixin_conversations.json"
            account_path = temp_path / "wechat-bot.json"
            context_tokens_path = temp_path / "wechat-bot.context-tokens.json"
            conversations_path.write_text(
                json.dumps(
                    {
                        "sender-a": {"current_session": "default"},
                        "sender-b": {"current_session": "focus"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            context_tokens_path.write_text(
                json.dumps({"sender-a": "ctx-a"}, ensure_ascii=False),
                encoding="utf-8",
            )

            with patch.object(weixin_notifier, "BRIDGE_CONVERSATIONS_PATH", conversations_path):
                recipients = weixin_notifier._load_recipients(account_path)

            self.assertEqual("sender-a", recipients[0].sender_id)
            self.assertEqual("ctx-a", recipients[0].context_token)
            self.assertEqual("sender-b", recipients[1].sender_id)
            self.assertEqual("", recipients[1].context_token)

    def test_broadcast_notice_reports_missing_context_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            conversations_path = temp_path / "weixin_conversations.json"
            account_path = temp_path / "wechat-bot.json"
            conversations_path.write_text(
                json.dumps({"sender-a": {"current_session": "default"}}, ensure_ascii=False),
                encoding="utf-8",
            )
            account_path.write_text(
                json.dumps({"token": "bot-token", "baseUrl": "https://example.com"}, ensure_ascii=False),
                encoding="utf-8",
            )
            cfg = BridgeConfig.load()
            cfg.account_file = str(account_path)
            cfg.service_notice_enabled = True

            with patch.object(weixin_notifier, "BRIDGE_CONVERSATIONS_PATH", conversations_path):
                result = weixin_notifier.broadcast_weixin_notice_by_kind("service", "服务操作", "启动完成", config=cfg)

            self.assertEqual(0, result.sent_count)
            self.assertEqual(1, result.recipient_count)
            self.assertIn("missing context token", result.error)


if __name__ == "__main__":
    unittest.main()
