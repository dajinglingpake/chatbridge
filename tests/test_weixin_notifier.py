from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
