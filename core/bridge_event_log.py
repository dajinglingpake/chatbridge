from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable


def append_bridge_event(path: Path, *, now: str, event: str, **payload: Any) -> None:
    _append_jsonl(
        path,
        {
            "at": now,
            "event": event,
            **payload,
        },
    )


def append_bridge_message_audit(path: Path, *, now: str, sender_id: str, text: str, route: str, **payload: Any) -> None:
    preview = " ".join(str(text or "").split())[:240]
    _append_jsonl(
        path,
        {
            "at": now,
            "sender_id": sender_id,
            "text": str(text or ""),
            "text_preview": preview,
            "route": route,
            **payload,
        },
    )


def load_recent_bridge_events(
    path: Path,
    *,
    sender_id: str = "",
    limit: int = 5,
    hidden: Callable[[dict[str, Any]], bool] | None = None,
) -> list[dict[str, str]]:
    if not path.exists():
        return []
    cleaned_sender_id = sender_id.strip()
    entries: list[dict[str, str]] = []
    for line in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict):
            continue
        raw_sender_id = str(raw.get("sender_id") or "").strip()
        if cleaned_sender_id and raw_sender_id != cleaned_sender_id:
            continue
        if hidden is not None and hidden(raw):
            continue
        entries.append({str(key): str(value) for key, value in raw.items() if value is not None})
        if len(entries) >= max(limit, 1):
            break
    return entries


def _append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
