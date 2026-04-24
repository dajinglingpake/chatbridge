from __future__ import annotations

from core.json_store import load_json, save_json
from core.runtime_paths import STATE_DIR
from core.weixin_send_gate import sender_send_lock


FAILED_DELIVERIES_PATH = STATE_DIR / "weixin_failed_deliveries.json"


def record_failed_delivery(
    *,
    to_user_id: str,
    context_token: str,
    text_preview: str,
    attempts: int,
    error: str,
) -> None:
    sender_id = str(to_user_id or "").strip()
    if not sender_id:
        return
    with sender_send_lock("__weixin_failed_deliveries__", timeout_seconds=15.0):
        payload = load_json(FAILED_DELIVERIES_PATH, {}, expect_type=dict)
        if not isinstance(payload, dict):
            payload = {}
        existing = payload.get(sender_id) if isinstance(payload.get(sender_id), dict) else {}
        count = int(existing.get("count") or 0) + 1
        payload[sender_id] = {
            "sender_id": sender_id,
            "context_token": str(context_token or "").strip(),
            "count": count,
            "attempts": int(attempts or 0),
            "error": str(error or "").strip(),
            "text_preview": str(text_preview or "").strip(),
        }
        save_json(FAILED_DELIVERIES_PATH, payload)


def pop_failed_delivery(sender_id: str) -> dict[str, object] | None:
    cleaned_sender_id = str(sender_id or "").strip()
    if not cleaned_sender_id:
        return None
    with sender_send_lock("__weixin_failed_deliveries__", timeout_seconds=15.0):
        payload = load_json(FAILED_DELIVERIES_PATH, {}, expect_type=dict)
        if not isinstance(payload, dict):
            return None
        entry = payload.pop(cleaned_sender_id, None)
        if payload:
            save_json(FAILED_DELIVERIES_PATH, payload)
        else:
            FAILED_DELIVERIES_PATH.unlink(missing_ok=True)
        return entry if isinstance(entry, dict) else None
