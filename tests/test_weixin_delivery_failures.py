from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.weixin_delivery_failures import pop_failed_delivery, record_failed_delivery


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


if __name__ == "__main__":
    unittest.main()
