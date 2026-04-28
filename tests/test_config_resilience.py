from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_hub import HubConfig
from bridge_config import BridgeConfig, WeixinAccountProfile, collapse_qr_bot_profiles
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

    def test_collapse_qr_bot_profiles_keeps_latest_qr_account(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_account = root / "old@im.bot.json"
            old_sync = root / "old@im.bot.sync.json"
            new_account = root / "new@im.bot.json"
            new_sync = root / "new@im.bot.sync.json"
            named_account = root / "named.json"
            named_sync = root / "named.sync.json"
            for path in [old_account, old_sync, new_account, new_sync, named_account, named_sync]:
                path.write_text("{}", encoding="utf-8")
            old_time = 1_700_000_000
            new_time = old_time + 10
            for path in [old_account, old_sync]:
                os.utime(path, (old_time, old_time))
            for path in [new_account, new_sync]:
                os.utime(path, (new_time, new_time))

            collapsed = collapse_qr_bot_profiles(
                [
                    WeixinAccountProfile("old@im.bot", str(old_account), str(old_sync)),
                    WeixinAccountProfile("new@im.bot", str(new_account), str(new_sync)),
                    WeixinAccountProfile("named", str(named_account), str(named_sync)),
                ]
            )

        self.assertEqual(["named", "new@im.bot"], [profile.account_id for profile in collapsed])

    def test_collapse_qr_bot_profiles_prefers_active_qr_account(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_account = root / "old@im.bot.json"
            old_sync = root / "old@im.bot.sync.json"
            new_account = root / "new@im.bot.json"
            new_sync = root / "new@im.bot.sync.json"
            for path in [old_account, old_sync, new_account, new_sync]:
                path.write_text("{}", encoding="utf-8")
            old_time = 1_700_000_020
            new_time = 1_700_000_010
            for path in [old_account, old_sync]:
                os.utime(path, (old_time, old_time))
            for path in [new_account, new_sync]:
                os.utime(path, (new_time, new_time))

            collapsed = collapse_qr_bot_profiles(
                [
                    WeixinAccountProfile("old@im.bot", str(old_account), str(old_sync)),
                    WeixinAccountProfile("new@im.bot", str(new_account), str(new_sync)),
                ],
                preferred_account_id="new@im.bot",
            )

        self.assertEqual(["new@im.bot"], [profile.account_id for profile in collapsed])

    def test_bridge_config_load_keeps_active_qr_account_when_old_context_is_newer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config" / "weixin_bridge.json"
            state_path = root / "accounts" / "bridge-account-state.local.json"
            old_account = root / "accounts" / "old@im.bot.json"
            old_sync = root / "accounts" / "old@im.bot.sync.json"
            old_context = root / "accounts" / "old@im.bot.context-tokens.json"
            new_account = root / "accounts" / "new@im.bot.json"
            new_sync = root / "accounts" / "new@im.bot.sync.json"
            for path in [config_path, state_path, old_account, old_sync, old_context, new_account, new_sync]:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}", encoding="utf-8")
            state_path.write_text(json.dumps({"active_account_id": "new@im.bot"}), encoding="utf-8")
            old_time = 1_700_000_030
            new_time = 1_700_000_020
            for path in [old_account, old_sync, old_context]:
                os.utime(path, (old_time, old_time))
            for path in [new_account, new_sync]:
                os.utime(path, (new_time, new_time))

            profiles = [
                WeixinAccountProfile("old@im.bot", str(old_account), str(old_sync)),
                WeixinAccountProfile("new@im.bot", str(new_account), str(new_sync)),
            ]
            with (
                patch("bridge_config.CONFIG_PATH", config_path),
                patch("bridge_config.ACCOUNT_STATE_PATH", state_path),
                patch("bridge_config.discover_account_profiles", return_value=profiles),
            ):
                config = BridgeConfig.load()

        self.assertEqual("new@im.bot", config.active_account_id)
        self.assertEqual(str(new_account), config.account_file)


if __name__ == "__main__":
    unittest.main()
