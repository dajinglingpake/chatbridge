from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from bridge_config import BridgeConfig
from runtime_stack import get_runtime_snapshot
from weixin_hub_bridge import WeixinBridge


DEFAULT_PROMPT = "列出所有会话"
DEFAULT_TIMEOUT_SECONDS = 90

class CaptureBridge(WeixinBridge):
    def __init__(self, config: BridgeConfig) -> None:
        self.sent_messages: list[dict[str, str]] = []
        super().__init__(config)

    def _send_text(self, base_url: str, token: str, to_user_id: str, context_token, text: str) -> None:
        self.sent_messages.append(
            {
                "to_user_id": to_user_id,
                "context_token": str(context_token or ""),
                "text": (text or "").strip(),
            }
        )

def _load_json_object(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_sender_events_since(event_log_path: Path, sender_id: str, *, start_line: int) -> list[dict[str, object]]:
    if not event_log_path.exists():
        return []
    lines = event_log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    matched: list[dict[str, object]] = []
    for line in lines[max(start_line, 0) :]:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("sender_id") or "").strip() != sender_id:
            continue
        matched.append(payload)
    return matched


def _prepare_sender_state(conversation_path: Path, sender_id: str) -> None:
    conversation_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _load_json_object(conversation_path)
    payload[sender_id] = {
        "current_session": "default",
        "sessions": {
            "default": {"backend": "codex"},
        },
    }
    conversation_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_incoming_message(*, sender_id: str, context_token: str, text: str, index: int) -> dict[str, object]:
    return {
        "message_type": 1,
        "from_user_id": sender_id,
        "context_token": context_token,
        "msg_id": f"smoke-{index}-{int(time.time())}",
        "create_time": int(time.time()),
        "item_list": [
            {
                "type": 1,
                "text_item": {"text": text},
            }
        ],
    }


def _wait_for_pending_tasks(bridge: CaptureBridge, *, timeout_seconds: int) -> bool:
    deadline = time.time() + max(timeout_seconds, 5)
    while bridge.pending_tasks and time.time() < deadline:
        bridge._poll_pending_tasks("https://example.com", "token")
        time.sleep(1.0)
    return not bridge.pending_tasks


def _seed_history(bridge: CaptureBridge, *, sender_id: str, context_token: str, timeout_seconds: int) -> bool:
    commands = [
        "帮我确认默认会话已经建立历史，简单回复一句即可。",
        "/new deep-dive",
        "帮我确认 deep-dive 会话也有历史，简单回复一句即可。",
    ]
    for index, text in enumerate(commands, start=1):
        bridge._handle_message(
            "https://example.com",
            "token",
            _build_incoming_message(sender_id=sender_id, context_token=context_token, text=text, index=index),
        )
        if text.startswith("/"):
            continue
        if not _wait_for_pending_tasks(bridge, timeout_seconds=timeout_seconds):
            return False
    return True


def _runtime_test_paths(temp_root: Path) -> tuple[Path, Path, Path]:
    state_dir = temp_root / ".runtime" / "state"
    log_dir = temp_root / ".runtime" / "logs"
    return (
        state_dir / "weixin_conversations.json",
        state_dir / "weixin_pending_tasks.json",
        log_dir / "weixin_bridge_events.jsonl",
    )


def run_smoke(*, prompt: str, sender_id: str, context_token: str, timeout_seconds: int, seed_history: bool) -> int:
    snapshot = get_runtime_snapshot()
    if not snapshot.hub_running:
        print("Smoke aborted: Hub is not running. Start ChatBridge first.", file=sys.stderr)
        return 2

    config = BridgeConfig.load()
    with tempfile.TemporaryDirectory(prefix="chatbridge-mgmt-smoke-") as temp_dir:
        temp_root = Path(temp_dir)
        conversation_path, pending_tasks_path, event_log_path = _runtime_test_paths(temp_root)
        saved_context_tokens: dict[str, str] = {}
        patchers = [
            patch.dict(os.environ, {"CHATBRIDGE_RUNTIME_ROOT": str(temp_root / ".runtime")}),
            patch("weixin_hub_bridge.CONVERSATION_PATH", conversation_path),
            patch("weixin_hub_bridge.PENDING_TASKS_PATH", pending_tasks_path),
            patch("weixin_hub_bridge.EVENT_LOG_PATH", event_log_path),
            patch("weixin_hub_bridge.load_account_context_tokens", side_effect=lambda _path: dict(saved_context_tokens)),
            patch(
                "weixin_hub_bridge.save_account_context_tokens",
                side_effect=lambda _path, tokens: saved_context_tokens.update(
                    {str(key).strip(): str(value).strip() for key, value in tokens.items()}
                ),
            ),
        ]
        with contextlib.ExitStack() as stack:
            for patcher in patchers:
                stack.enter_context(patcher)

            conversation_path.parent.mkdir(parents=True, exist_ok=True)
            pending_tasks_path.parent.mkdir(parents=True, exist_ok=True)
            event_log_path.parent.mkdir(parents=True, exist_ok=True)
            _prepare_sender_state(conversation_path, sender_id)
            bridge = CaptureBridge(config)
            original_event_line_count = 0
            if seed_history:
                print("SEED_HISTORY=True")
                if not _seed_history(bridge, sender_id=sender_id, context_token=context_token, timeout_seconds=timeout_seconds):
                    print("Smoke failed: history seeding did not finish before timeout.", file=sys.stderr)
                    return 1
                bridge.sent_messages.clear()
                original_event_line_count = len(event_log_path.read_text(encoding="utf-8", errors="replace").splitlines()) if event_log_path.exists() else 0

            bridge._handle_message(
                "https://example.com",
                "token",
                _build_incoming_message(sender_id=sender_id, context_token=context_token, text=prompt, index=999),
            )
            if not _wait_for_pending_tasks(bridge, timeout_seconds=timeout_seconds):
                print("Smoke failed: pending task did not finish before timeout.", file=sys.stderr)
                return 1

            print(f"PENDING_EMPTY={not bridge.pending_tasks}")
            print(f"SENT_COUNT={len(bridge.sent_messages)}")
            for index, item in enumerate(bridge.sent_messages, start=1):
                print(f"--- MESSAGE {index} ---")
                print(item["text"])

            print("--- EVENTS ---")
            for event in _load_sender_events_since(event_log_path, sender_id, start_line=original_event_line_count):
                print(json.dumps(event, ensure_ascii=False))

            if not bridge.sent_messages:
                print("Smoke failed: no reply captured.", file=sys.stderr)
                return 1
            return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Developer smoke test for the ChatBridge sender-session path.")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="Synthetic incoming WeChat message text.")
    parser.add_argument("--sender-id", default="", help="Synthetic sender id. Defaults to a unique ephemeral sender for each run.")
    parser.add_argument("--context-token", default="", help="Synthetic context token. Defaults to a unique ephemeral token for each run.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Wait timeout in seconds.")
    parser.add_argument("--seed-history", action="store_true", help="Seed two real session replies before asking the sender-session path.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_suffix = str(int(time.time() * 1000))
    sender_id = str(args.sender_id).strip() or f"sender-smoke-{run_suffix}@local"
    context_token = str(args.context_token).strip() or f"sender-smoke-context-{run_suffix}"
    return run_smoke(
        prompt=str(args.prompt),
        sender_id=sender_id,
        context_token=context_token,
        timeout_seconds=int(args.timeout),
        seed_history=bool(args.seed_history),
    )


if __name__ == "__main__":
    raise SystemExit(main())
