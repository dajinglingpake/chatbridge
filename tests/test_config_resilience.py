from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_hub import HubConfig
from bridge_config import BridgeConfig, WeixinAccountProfile
from core.accounts import DEFAULT_ILINK_BASE_URL, resolve_ilink_base_url


class ConfigResilienceTests(unittest.TestCase):
    def test_bridge_config_load_recovers_from_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            accounts_dir = root / "accounts"
            accounts_dir.mkdir(parents=True, exist_ok=True)
            config_path = root / "config" / "weixin_bridge.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text("{invalid", encoding="utf-8")
            state_path = accounts_dir / "bridge-account-state.local.json"

            with (
                patch("bridge_config.APP_DIR", root),
                patch("bridge_config.WEIXIN_ACCOUNTS_DIR", accounts_dir),
                patch("bridge_config.CONFIG_PATH", config_path),
                patch("bridge_config.ACCOUNT_STATE_PATH", state_path),
            ):
                config = BridgeConfig.load()

            self.assertEqual("wechat-bot", config.active_account_id)
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual("main", saved["backend_id"])

    def test_hub_config_load_recovers_from_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config" / "agent_hub.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text("{invalid", encoding="utf-8")
            workspace_dir = root / "workspace"
            session_dir = root / "sessions"

            with (
                patch("agent_hub.CONFIG_PATH", config_path),
                patch("agent_hub.WORKSPACE_DIR", workspace_dir),
                patch("agent_hub.SESSION_DIR", session_dir),
            ):
                config = HubConfig.load()

            self.assertEqual(1, len(config.agents))
            self.assertEqual("main", config.agents[0].id)
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual("main", saved["agents"][0]["id"])

    def test_resolve_ilink_base_url_uses_active_account(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            account_a = root / "account-a.json"
            account_b = root / "account-b.json"
            account_a.write_text(json.dumps({"baseUrl": "https://example-a.test"}), encoding="utf-8")
            account_b.write_text(json.dumps({"baseUrl": "https://example-b.test"}), encoding="utf-8")

            config = BridgeConfig(
                active_account_id="wechat-b",
                accounts=[
                    WeixinAccountProfile("wechat-a", str(account_a), str(root / "account-a.sync.json")),
                    WeixinAccountProfile("wechat-b", str(account_b), str(root / "account-b.sync.json")),
                ],
            )

            self.assertEqual("https://example-b.test", resolve_ilink_base_url(config))

    def test_resolve_ilink_base_url_falls_back_when_missing(self) -> None:
        config = BridgeConfig(
            active_account_id="wechat-b",
            accounts=[
                WeixinAccountProfile("wechat-b", "missing-account.json", "missing-sync.json"),
            ],
        )

        self.assertEqual(DEFAULT_ILINK_BASE_URL, resolve_ilink_base_url(config))


if __name__ == "__main__":
    unittest.main()
