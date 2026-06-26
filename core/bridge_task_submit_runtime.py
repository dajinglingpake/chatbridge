from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.bridge_runtime import BridgeSubmittedTask, IncomingBridgeMessage


@dataclass(frozen=True)
class BridgeTaskSubmitContext:
    agent_id: str
    session_name: str
    backend: str
    workdir: str = ""
    model: str = ""
    reasoning_effort: str = ""
    permission_mode: str = ""
    bridge_conversations_path: str = ""
    bridge_event_log_path: str = ""
    context_token: str = ""


class BridgeTaskSubmitRuntime:
    def __init__(
        self,
        *,
        ipc_request: Callable[[str, dict[str, Any], float], Any],
        resolve_context: Callable[[IncomingBridgeMessage, Any], BridgeTaskSubmitContext],
        timeout_seconds: float = 60,
        on_complete: Callable[[IncomingBridgeMessage, BridgeTaskSubmitContext, float, Any], None] | None = None,
    ) -> None:
        self.ipc_request = ipc_request
        self.resolve_context = resolve_context
        self.timeout_seconds = timeout_seconds
        self.on_complete = on_complete

    def submit(self, message: IncomingBridgeMessage, session: Any, prompt: str) -> BridgeSubmittedTask:
        context = self.resolve_context(message, session)
        payload = self._build_payload(message, context, prompt)
        started_at = time.perf_counter()
        response = self.ipc_request("submit_task", payload, self.timeout_seconds)
        if self.on_complete is not None:
            self.on_complete(message, context, started_at, response)
        if not response.ok:
            raise RuntimeError(str(response.error or "submit_task failed"))
        task = response.payload.get("task") or {}
        if not isinstance(task, dict):
            task = {}
        task_id = str(task.get("id") or "").strip()
        if not task_id:
            raise RuntimeError("submit_task returned invalid task payload")
        return BridgeSubmittedTask(task_id=task_id, payload=task)

    @staticmethod
    def _build_payload(message: IncomingBridgeMessage, context: BridgeTaskSubmitContext, prompt: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "agent_id": context.agent_id,
            "prompt": prompt,
            "source": message.source,
            "sender_id": message.sender_id,
            "session_name": context.session_name,
            "backend": context.backend,
        }
        optional_fields = {
            "workdir": context.workdir,
            "model": context.model,
            "reasoning_effort": context.reasoning_effort,
            "permission_mode": context.permission_mode,
            "bridge_conversations_path": context.bridge_conversations_path,
            "bridge_event_log_path": context.bridge_event_log_path,
            "context_token": context.context_token or message.context_token,
        }
        for key, value in optional_fields.items():
            if str(value or "").strip():
                payload[key] = value
        return payload
