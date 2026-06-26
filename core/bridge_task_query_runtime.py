from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.state_models import HubTask


class BridgeTaskQueryRuntime:
    def __init__(
        self,
        *,
        ipc_request: Callable[[str, dict[str, Any], float], Any],
        default_backend: Callable[[], str],
        timeout_seconds: float = 5,
    ) -> None:
        self.ipc_request = ipc_request
        self.default_backend = default_backend
        self.timeout_seconds = timeout_seconds

    def get_task(self, task_id: str) -> HubTask | None:
        response = self.ipc_request("get_task", {"task_id": task_id}, self.timeout_seconds)
        if not response.ok:
            return None
        return HubTask.from_dict(response.payload.get("task"), default_backend=self.default_backend())

    def load_sender_tasks(self, sender_id: str) -> list[HubTask]:
        response = self.ipc_request("state", {}, self.timeout_seconds)
        if not response.ok:
            return []
        sender_tasks: list[HubTask] = []
        for raw_task in response.payload.get("tasks") or []:
            task = HubTask.from_dict(raw_task, default_backend=self.default_backend())
            if task is None or task.sender_id != sender_id:
                continue
            sender_tasks.append(task)
        return sorted(
            sender_tasks,
            key=lambda item: item.finished_at or item.started_at or item.created_at,
            reverse=True,
        )

    def find_latest_sender_task(self, sender_id: str, *, allowed_statuses: set[str] | None = None) -> HubTask | None:
        sender_tasks = self.load_sender_tasks(sender_id)
        if allowed_statuses is not None:
            sender_tasks = [task for task in sender_tasks if task.status in allowed_statuses]
        return sender_tasks[0] if sender_tasks else None
