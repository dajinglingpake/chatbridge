from __future__ import annotations

import json
import unittest

from bridge_config import BridgeConfig
from runtime_stack import BRIDGE_CONVERSATIONS_PATH
from weixin_hub_bridge import WeixinBridge


class FakeBridge(WeixinBridge):
    def __init__(self, config: BridgeConfig) -> None:
        super().__init__(config)
        self._state_payload = {
            "ok": True,
            "tasks": [
                {
                    "id": "task-test-001",
                    "sender_id": "sender-test",
                    "session_name": "default",
                    "status": "succeeded",
                    "agent_name": "default",
                    "backend": "codex",
                    "prompt": "hello",
                    "output": "world",
                }
            ],
        }
        self._task_payload = {"ok": True, "task": self._state_payload["tasks"][0]}

    def _ipc_request(self, action: str, payload: dict[str, object], timeout_seconds: float) -> dict[str, object]:
        if action == "state":
            return self._state_payload
        if action == "get_task":
            task_id = str(payload.get("task_id") or "")
            if task_id == "task-test-001":
                return self._task_payload
            return {"ok": False, "error": "task not found"}
        raise RuntimeError(f"unexpected action: {action}")

    def _save_conversations(self) -> None:
        return None


class WeixinBridgeCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        BRIDGE_CONVERSATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        BRIDGE_CONVERSATIONS_PATH.write_text(
            json.dumps(
                {
                    "sender-test": {
                        "current_session": "default",
                        "sessions": {"default": {"backend": "codex"}},
                    }
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self.bridge = FakeBridge(BridgeConfig.load())

    def test_notify_command_renders_multiline_status(self) -> None:
        reply, handled = self.bridge._handle_control_command("sender-test", "/notify")
        self.assertTrue(handled)
        self.assertIn("Current system notices", reply)
        self.assertIn("Service lifecycle:", reply)
        self.assertNotIn("\\n", reply)

    def test_task_lookup_command_returns_summary(self) -> None:
        reply, handled = self.bridge._handle_control_command("sender-test", "/task task-test-001")
        self.assertTrue(handled)
        self.assertIn("Task ID: task-test-001", reply)
        self.assertIn("Prompt summary:", reply)
        self.assertIn("Output/Error summary:", reply)

    def test_last_command_uses_sender_scope(self) -> None:
        reply, handled = self.bridge._handle_control_command("sender-test", "/last")
        self.assertTrue(handled)
        self.assertIn("Task ID: task-test-001", reply)
        self.assertIn("Status: succeeded", reply)

    def test_backend_switch_command_updates_current_session_backend(self) -> None:
        reply, handled = self.bridge._handle_control_command("sender-test", "/backend claude")
        self.assertTrue(handled)
        self.assertIn("claude", reply)
        binding = self.bridge.conversations["sender-test"]
        self.assertEqual(binding["sessions"]["default"]["backend"], "claude")


if __name__ == "__main__":
    unittest.main()
