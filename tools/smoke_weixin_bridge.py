from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from bridge_config import BridgeConfig
from core.state_models import IpcResponseEnvelope
from core.weixin_notifier import build_task_followup_hint
from weixin_hub_bridge import WeixinBridge


class SmokeBridge(WeixinBridge):
    def __init__(self, config: BridgeConfig) -> None:
        super().__init__(config)
        self._state_payload = {
            "tasks": [
                {
                    "id": "task-smoke-001",
                    "sender_id": "sender-smoke",
                    "session_name": "default",
                    "status": "succeeded",
                    "agent_name": "default",
                    "backend": "codex",
                    "prompt": "hello smoke",
                    "output": "smoke ok",
                }
            ],
        }
        self._task_payload = self._state_payload["tasks"][0]

    def _ipc_request(self, action: str, payload: dict[str, object], timeout_seconds: float) -> IpcResponseEnvelope:
        if action == "state":
            return IpcResponseEnvelope(ok=True, payload=self._state_payload)
        if action == "get_task":
            task_id = str(payload.get("task_id") or "")
            if task_id == "task-smoke-001":
                return IpcResponseEnvelope(ok=True, payload={"task": self._task_payload})
            return IpcResponseEnvelope(ok=False, error="task not found")
        raise RuntimeError(f"unexpected action: {action}")

    def _save_conversations(self) -> None:
        return None


def assert_contains(text: str, expected: str) -> None:
    if expected not in text:
        raise AssertionError(f"expected {expected!r} in {text!r}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="chatbridge-bridge-smoke-") as temp_dir:
        temp_root = Path(temp_dir)
        runtime_root = temp_root / ".runtime"
        conversation_path = runtime_root / "state" / "weixin_conversations.json"
        pending_tasks_path = runtime_root / "state" / "weixin_pending_tasks.json"
        event_log_path = runtime_root / "logs" / "weixin_bridge_events.jsonl"
        state_path = runtime_root / "state" / "weixin_hub_bridge_state.json"

        conversation_path.parent.mkdir(parents=True, exist_ok=True)
        event_log_path.parent.mkdir(parents=True, exist_ok=True)
        conversation_path.write_text(
            json.dumps(
                {
                    "sender-smoke": {
                        "current_session": "default",
                        "sessions": {"default": {"backend": "codex"}},
                    }
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        with (
            patch.dict(os.environ, {"CHATBRIDGE_LANG": "en-US", "CHATBRIDGE_RUNTIME_ROOT": str(runtime_root)}),
            patch("weixin_hub_bridge.CONVERSATION_PATH", conversation_path),
            patch("weixin_hub_bridge.PENDING_TASKS_PATH", pending_tasks_path),
            patch("weixin_hub_bridge.EVENT_LOG_PATH", event_log_path),
            patch("weixin_hub_bridge.STATE_PATH", state_path),
            patch("weixin_hub_bridge.load_account_context_tokens", return_value={}),
            patch("weixin_hub_bridge.save_account_context_tokens", return_value=None),
        ):
            bridge = SmokeBridge(BridgeConfig.load())

            checks = [
                ("/notify", ["Current system notices", "Service lifecycle:"]),
                ("/agent", ["Current assistant:"]),
                ("/backend", ["Current setup", "Current session:", "Current backend:"]),
                ("/backend claude", ["Switched backend for current session:", "claude"]),
                ("/task task-smoke-001", ["Task details", "Task ID: task-smoke-001", "Prompt summary:", "Result summary:"]),
                ("/last", ["Task details", "Task ID: task-smoke-001", "Status: Completed"]),
            ]

            for command, expected_parts in checks:
                reply, handled = bridge._handle_control_command("sender-smoke", command)
                if not handled:
                    raise AssertionError(f"command should be handled: {command}")
                for expected in expected_parts:
                    assert_contains(reply, expected)

            hint = build_task_followup_hint("task-smoke-001", "default")
            assert_contains(hint, "/task task-smoke-001")
            assert_contains(hint, "/last")
            assert_contains(hint, "当前会话: default")

            print("Smoke validation passed:")
            for command, _ in checks:
                print(f"- {command}")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
