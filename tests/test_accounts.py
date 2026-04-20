from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge_config import BridgeConfig
from core.accounts import AccountFilePayload, DEFAULT_ILINK_BASE_URL, QRConfirmedPayload, load_account_file_payload, save_account_from_qr_payload


class AccountPayloadTests(unittest.TestCase):
    def test_account_file_payload_from_dict_normalizes_fields(self) -> None:
        payload = AccountFilePayload.from_dict(
            {
                "token": " abc ",
                "baseUrl": " https://example.test ",
                "name": " bot-a ",
            }
        )

        self.assertEqual("abc", payload.token)
        self.assertEqual("https://example.test", payload.base_url)
        self.assertEqual("bot-a", payload.name)

    def test_qr_confirmed_payload_from_dict_applies_fallbacks(self) -> None:
        payload = QRConfirmedPayload.from_dict(
            {
                "ilink_bot_id": " wechat-a ",
                "bot_token": " token-1 ",
            },
            fallback_base_url="https://fallback.test",
        )

        self.assertEqual("wechat-a", payload.account_id)
        self.assertEqual("token-1", payload.bot_token)
        self.assertEqual("https://fallback.test", payload.base_url)

    def test_load_account_file_payload_returns_default_for_missing_file(self) -> None:
        payload = load_account_file_payload(Path("missing-account.json"))
        self.assertEqual("", payload.token)
        self.assertEqual("", payload.base_url)

    def test_save_account_from_qr_payload_persists_typed_account_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            accounts_dir = root / "accounts"
            accounts_dir.mkdir(parents=True, exist_ok=True)
            config = BridgeConfig()

            with (
                patch("core.accounts.APP_DIR", root),
                patch.object(BridgeConfig, "load", return_value=config),
                patch.object(config, "save", return_value=None),
            ):
                saved = save_account_from_qr_payload(
                    {
                        "ilink_bot_id": "wechat-typed",
                        "bot_token": "typed-token",
                    },
                    base_url=DEFAULT_ILINK_BASE_URL,
                    config=config,
                )

            self.assertIsNotNone(saved)
            assert saved is not None
            payload = json.loads(saved.account_file.read_text(encoding="utf-8"))
            self.assertEqual(
                {
                    "token": "typed-token",
                    "baseUrl": DEFAULT_ILINK_BASE_URL,
                    "name": "wechat-typed",
                },
                payload,
            )


if __name__ == "__main__":
    unittest.main()
