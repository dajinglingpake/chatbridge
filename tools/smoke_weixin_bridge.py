from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from bridge_config import BridgeConfig
from core.weixin_notifier import build_task_followup_hint
from runtime_stack import BRIDGE_CONVERSATIONS_PATH
from weixin_hub_bridge import WeixinBridge


class SmokeBridge(WeixinBridge):
    def __init__(self, config: BridgeConfig) -> None:
        super().__init__(config)
        self._state_payload = {
            "ok": True,
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
        self._task_payload = {"ok": True, "task": self._state_payload["tasks"][0]}

    def _ipc_request(self, action: str, payload: dict[str, object], timeout_seconds: float) -> dict[str, object]:
        if action == "state":
            return self._state_payload
        if action == "get_task":
            task_id = str(payload.get("task_id") or "")
            if task_id == "task-smoke-001":
                return self._task_payload
            return {"ok": False, "error": "task not found"}
        raise RuntimeError(f"unexpected action: {action}")

    def _save_conversations(self) -> None:
        return None


def assert_contains(text: str, expected: str) -> None:
    if expected not in text:
        raise AssertionError(f"expected {expected!r} in {text!r}")


def main() -> int:
    BRIDGE_CONVERSATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    BRIDGE_CONVERSATIONS_PATH.write_text(
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

    bridge = SmokeBridge(BridgeConfig.load())

    checks = [
        ("/notify", ["Current system notices", "Service lifecycle:"]),
        ("/agent", ["Current bridge agent:"]),
        ("/backend", ["Current session:", "Current backend:"]),
        ("/backend claude", ["Switched backend for current session:", "claude"]),
        ("/task task-smoke-001", ["Task ID: task-smoke-001", "Prompt summary:", "Output/Error summary:"]),
        ("/last", ["Task ID: task-smoke-001", "Status: succeeded"]),
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
