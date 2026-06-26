from __future__ import annotations

from typing import Any, Callable

from core.bridge_command_catalog import parse_bridge_command, render_bridge_help
from core.bridge_runtime import BridgeCommandResult
from core.state_models import HubTask

class BridgeControlRuntime:
    def __init__(
        self,
        *,
        help_message_keys: tuple[str, ...],
        translate: Callable[..., str],
        default_backend: Callable[[], str],
        agent_id: Callable[[], str],
        session_name: Callable[[str], str],
        get_task: Callable[[str], HubTask | None],
        find_latest_sender_task: Callable[[str, set[str] | None], HubTask | None],
        ipc_request: Callable[[str, dict[str, Any], float], Any],
        retry_source: str,
        unsupported_message: str | None,
        render_status_reply: Callable[[str], str] | None = None,
        render_task_summary_reply: Callable[[HubTask], str] | None = None,
        restrict_task_lookup_to_sender: bool = True,
    ) -> None:
        self.help_message_keys = help_message_keys
        self.translate = translate
        self.default_backend = default_backend
        self.agent_id = agent_id
        self.session_name = session_name
        self.get_task = get_task
        self.find_latest_sender_task = find_latest_sender_task
        self.ipc_request = ipc_request
        self.retry_source = retry_source
        self.unsupported_message = unsupported_message
        self.render_status_reply = render_status_reply
        self.render_task_summary_reply = render_task_summary_reply
        self.restrict_task_lookup_to_sender = restrict_task_lookup_to_sender

    def handle(self, sender_id: str, text: str) -> BridgeCommandResult:
        parsed = parse_bridge_command(text)
        if parsed is None:
            return BridgeCommandResult(False)
        if parsed.is_passthrough:
            if parsed.passthrough_prompt.strip().lower() == "/status":
                return BridgeCommandResult(True, self.render_codex_status(sender_id))
            return BridgeCommandResult(False)

        command = parsed.command
        parts = list(parsed.parts)
        if command in {"/help", "/h", "/?"}:
            return BridgeCommandResult(True, render_bridge_help(self.translate, self.help_message_keys))
        if command == "/status":
            if self.render_status_reply is not None:
                return BridgeCommandResult(True, self.render_status_reply(sender_id))
            return BridgeCommandResult(True, self.render_status(sender_id))
        if command == "/task":
            task_id = parts[1].strip() if len(parts) >= 2 else ""
            if not task_id:
                return BridgeCommandResult(True, self.translate("bridge.task.lookup.usage"))
            task = self.get_task(task_id)
            if task is None or (self.restrict_task_lookup_to_sender and task.sender_id != sender_id):
                return BridgeCommandResult(True, self.translate("bridge.task.lookup.not_found", task_id=task_id))
            return BridgeCommandResult(True, self.render_task_summary(task))
        if command == "/last":
            task = self.find_latest_sender_task(sender_id, None)
            if task is None:
                return BridgeCommandResult(True, self.translate("bridge.task.lookup.none"))
            return BridgeCommandResult(True, self.render_task_summary(task))
        if command == "/cancel":
            task = self.resolve_sender_task(sender_id, parts[1].strip() if len(parts) >= 2 else "", allowed_statuses={"queued", "running"})
            if task is None:
                return BridgeCommandResult(True, self.translate("bridge.task.cancel.none"))
            response = self.ipc_request("cancel_task", {"task_id": task.id}, 5)
            if not response.ok:
                return BridgeCommandResult(True, self.translate("bridge.task.cancel.failed", task_id=task.id, error=str(response.error or "unknown error")))
            canceled = HubTask.from_dict(response.payload.get("task"), default_backend=self.default_backend())
            if canceled is None:
                return BridgeCommandResult(True, self.translate("bridge.task.cancel.failed", task_id=task.id, error="invalid task payload"))
            return BridgeCommandResult(True, self.translate("bridge.task.cancel.ok", task_id=canceled.id, session=canceled.session_name or self.session_name(sender_id)))
        if command == "/retry":
            task = self.resolve_sender_task(sender_id, parts[1].strip() if len(parts) >= 2 else "")
            if task is None:
                return BridgeCommandResult(True, self.translate("bridge.task.retry.none"))
            response = self.ipc_request("retry_task", {"task_id": task.id, "source": self.retry_source, "sender_id": sender_id}, 5)
            if not response.ok:
                return BridgeCommandResult(True, self.translate("bridge.task.retry.failed", task_id=task.id, error=str(response.error or "unknown error")))
            retried = HubTask.from_dict(response.payload.get("task"), default_backend=self.default_backend())
            if retried is None:
                return BridgeCommandResult(True, self.translate("bridge.task.retry.failed", task_id=task.id, error="invalid task payload"))
            return BridgeCommandResult(
                True,
                self.translate(
                    "bridge.task.retry.ok",
                    original=task.id,
                    task_id=retried.id,
                    session=retried.session_name or self.session_name(sender_id),
                    backend=retried.backend or self.default_backend(),
                ),
            )
        if self.unsupported_message is None:
            return BridgeCommandResult(False)
        return BridgeCommandResult(True, self.unsupported_message)

    def resolve_sender_task(self, sender_id: str, task_id: str, *, allowed_statuses: set[str] | None = None) -> HubTask | None:
        cleaned_id = task_id.strip()
        if cleaned_id:
            task = self.get_task(cleaned_id)
            if task is None or task.sender_id != sender_id:
                return None
            if allowed_statuses is not None and task.status not in allowed_statuses:
                return None
            return task
        return self.find_latest_sender_task(sender_id, allowed_statuses)

    def render_status(self, sender_id: str) -> str:
        latest_task = self.find_latest_sender_task(sender_id, None)
        latest_line = "-"
        if latest_task is not None:
            latest_line = f"{latest_task.id} [{self.display_task_status(latest_task.status)}]"
        return "\n".join(
            [
                "当前设置",
                f"当前助手: {self.agent_id()}",
                f"当前后端: {self.default_backend()}",
                f"当前会话: {self.session_name(sender_id)}",
                f"最近任务: {latest_line}",
            ]
        )

    def render_codex_status(self, sender_id: str) -> str:
        if self.default_backend() != "codex":
            return "当前会话后端不是 Codex，//status 只支持 Codex 会话。"
        response = self.ipc_request(
            "codex_status",
            {
                "agent_id": self.agent_id(),
                "session_name": self.session_name(sender_id),
                "workdir": "",
            },
            15,
        )
        if not response.ok:
            return f"Codex 状态查询失败：{response.error or 'unknown error'}"
        status_panel = str(response.payload.get("status") or "").strip()
        if not status_panel:
            return "当前会话还没有可查询的 Codex 交互状态。请先在这个会话里发送一条普通消息。"
        return status_panel

    def render_task_summary(self, task: HubTask) -> str:
        if self.render_task_summary_reply is not None:
            return self.render_task_summary_reply(task)
        prompt = task.prompt.strip()[:400] or "(empty)"
        result = (task.output or task.error).strip()[:800] or "(empty)"
        return self.translate(
            "bridge.task.lookup.summary",
            task_id=task.id,
            session=task.session_name or "default",
            status=self.display_task_status(task.status),
            agent=task.agent_name or task.agent_id,
            backend=task.backend or self.default_backend(),
            model=task.model.strip() or "-",
            prompt=prompt,
            result=result,
        )

    def display_task_status(self, status: str) -> str:
        cleaned = str(status or "").strip().lower()
        return self.translate(f"bridge.task.status.{cleaned}") if cleaned else self.translate("bridge.task.status.unknown")
