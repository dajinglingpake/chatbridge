from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from core.runtime_paths import STATE_DIR
from core.weixin_send_gate import sender_send_lock


OUTBOX_PATH = STATE_DIR / "weixin_text_outbox.jsonl"
RETRY_BASE_SECONDS = 2
RETRY_MAX_SECONDS = 60
MAX_RETRY_ATTEMPTS = 6


def enqueue_text_message(*, to_user_id: str, context_token: str, text: str, source: str = "") -> None:
    payload = {
        "id": uuid.uuid4().hex,
        "to_user_id": str(to_user_id or "").strip(),
        "context_token": str(context_token or "").strip(),
        "text": str(text or ""),
        "source": str(source or "").strip(),
        "attempt": 0,
        "created_at": int(time.time()),
        "retry_not_before": 0,
    }
    _append_payload(payload)


def requeue_text_message(payload: dict[str, object]) -> None:
    retry_payload = dict(payload)
    next_attempt = int(retry_payload.get("attempt") or 0) + 1
    delay_seconds = min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * (2 ** max(0, next_attempt - 1)))
    retry_payload["attempt"] = next_attempt
    retry_payload["retry_not_before"] = int(time.time()) + delay_seconds
    _append_payload(retry_payload)


def pop_text_messages(*, limit: int = 20) -> list[dict[str, object]]:
    OUTBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sender_send_lock("__weixin_text_outbox__", timeout_seconds=15.0):
        if not OUTBOX_PATH.exists():
            return []
        raw_lines = OUTBOX_PATH.read_text(encoding="utf-8").splitlines()
        messages: list[dict[str, object]] = []
        remainder: list[str] = []
        now_seconds = int(time.time())
        for raw_line in raw_lines:
            if not raw_line.strip():
                continue
            if len(messages) < limit:
                try:
                    payload = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    retry_not_before = int(payload.get("retry_not_before") or 0)
                    if retry_not_before > now_seconds:
                        remainder.append(raw_line)
                        continue
                    messages.append(payload)
                    continue
            remainder.append(raw_line)
        if remainder:
            OUTBOX_PATH.write_text("\n".join(remainder) + "\n", encoding="utf-8")
        else:
            OUTBOX_PATH.unlink(missing_ok=True)
        return messages


def _append_payload(payload: dict[str, object]) -> None:
    OUTBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    with sender_send_lock("__weixin_text_outbox__", timeout_seconds=15.0):
        with OUTBOX_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line)
