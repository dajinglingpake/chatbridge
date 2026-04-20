from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge_config import BridgeConfig
from core import app_service
from core.state_models import RuntimeSnapshot
from core.weixin_notifier import NoticeResult


def _runtime_snapshot(*, bridge_running: bool) -> RuntimeSnapshot:
    return RuntimeSnapshot(
        hub_running=False,
        bridge_running=bridge_running,
        hub_pid=None,
        bridge_pid=202 if bridge_running else None,
        codex_processes=[],
        log_dir=".runtime/logs",
    )


def _bridge_config(*, default_backend: str = "codex") -> BridgeConfig:
    return BridgeConfig(default_backend=default_backend)


def _notice_result(summary_error: str = "") -> NoticeResult:
    return NoticeResult(sent_count=0, recipient_count=0, error=summary_error)


class AppServiceConversationTests(unittest.TestCase):
    def test_switch_weixin_session_backend_updates_current_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conversations_path = Path(temp_dir) / "weixin_conversations.json"
            conversations_path.write_text(
                json.dumps(
                    {
                        "sender-a": {
                            "current_session": "focus",
                            "sessions": {
                                "focus": {"backend": "codex"},
                            },
                        }
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            with (
                patch.object(app_service, "BRIDGE_CONVERSATIONS_PATH", conversations_path),
                patch.object(app_service.BridgeConfig, "load", return_value=_bridge_config(default_backend="codex")),
                patch.object(app_service, "get_runtime_snapshot", return_value=_runtime_snapshot(bridge_running=False)),
                patch.object(app_service, "broadcast_weixin_notice_by_kind", return_value=_notice_result()),
            ):
                result = app_service.switch_weixin_session_backend("sender-a", "claude")

            self.assertTrue(result.ok)
            payload = json.loads(conversations_path.read_text(encoding="utf-8"))
            self.assertEqual("claude", payload["sender-a"]["sessions"]["focus"]["backend"])

    def test_reset_weixin_conversation_removes_sender_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conversations_path = Path(temp_dir) / "weixin_conversations.json"
            conversations_path.write_text(
                json.dumps(
                    {
                        "sender-a": {
                            "current_session": "default",
                            "sessions": {
                                "default": {"backend": "codex"},
                            },
                        },
                        "sender-b": {
                            "current_session": "default",
                            "sessions": {
                                "default": {"backend": "claude"},
                            },
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            with (
                patch.object(app_service, "BRIDGE_CONVERSATIONS_PATH", conversations_path),
                patch.object(app_service, "get_runtime_snapshot", return_value=_runtime_snapshot(bridge_running=False)),
                patch.object(app_service, "broadcast_weixin_notice_by_kind", return_value=_notice_result()),
            ):
                result = app_service.reset_weixin_conversation("sender-a")

            self.assertTrue(result.ok)
            payload = json.loads(conversations_path.read_text(encoding="utf-8"))
            self.assertNotIn("sender-a", payload)
            self.assertIn("sender-b", payload)


if __name__ == "__main__":
    unittest.main()
