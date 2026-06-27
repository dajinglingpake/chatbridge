from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Generic, TypeVar

from core.json_store import load_json, save_json


T = TypeVar("T")


class JsonBackedTaskStore(Generic[T]):
    def __init__(
        self,
        path: Path,
        *,
        from_dict: Callable[[object], T | None],
        to_dict: Callable[[T], dict[str, Any]],
    ) -> None:
        self.path = path
        self._from_dict = from_dict
        self._to_dict = to_dict

    def load(self) -> dict[str, T]:
        data = load_json(self.path, {}, expect_type=dict)
        if not isinstance(data, dict):
            return {}
        tasks: dict[str, T] = {}
        for task_id, raw_task in data.items():
            task = self._from_dict(raw_task)
            if task is not None:
                tasks[str(task_id)] = task
        return tasks

    def save(self, tasks: dict[str, T]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        save_json(self.path, {task_id: self._to_dict(task) for task_id, task in tasks.items()})


@dataclass
class BridgePendingReplyTask:
    task_id: str
    sender_key: str
    reply_target: dict[str, Any]
    created_at: int
    last_progress_seq: int = 0
    last_progress_text: str = ""
    interrupt_base_prompt: str = ""
    interrupt_messages: list[str] = field(default_factory=list)
    restart_resume_count: int = 0

    @classmethod
    def from_dict(cls, raw: object, *, ttl_seconds: int | None = None, now_seconds: int | None = None) -> "BridgePendingReplyTask | None":
        if not isinstance(raw, dict):
            return None
        task_id = str(raw.get("task_id") or "").strip()
        sender_key = str(raw.get("sender_key") or "").strip()
        reply_target = raw.get("reply_target") if isinstance(raw.get("reply_target"), dict) else raw.get("event")
        if not task_id or not sender_key or not isinstance(reply_target, dict) or not reply_target:
            return None
        try:
            created_at = int(raw.get("created_at") or 0)
        except (TypeError, ValueError):
            created_at = 0
        current = int(time.time()) if now_seconds is None else now_seconds
        if ttl_seconds is not None and created_at and current - created_at > ttl_seconds:
            return None
        return cls(
            task_id=task_id,
            sender_key=sender_key,
            reply_target=dict(reply_target),
            created_at=created_at or current,
            last_progress_seq=int(raw.get("last_progress_seq") or 0),
            last_progress_text=str(raw.get("last_progress_text") or ""),
            interrupt_base_prompt=str(raw.get("interrupt_base_prompt") or ""),
            interrupt_messages=[str(item) for item in raw.get("interrupt_messages", []) if str(item or "").strip()]
            if isinstance(raw.get("interrupt_messages"), list)
            else [],
            restart_resume_count=int(raw.get("restart_resume_count") or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
