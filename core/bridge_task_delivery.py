from __future__ import annotations

import time
from typing import Any, Callable

from core.bridge_message_control import extract_progress_delta, normalize_message_for_dedupe
from core.state_models import HubTask
from core.weixin_message_format import format_duration_since, prefix_weixin_output


TERMINAL_TASK_STATUSES = frozenset({"succeeded", "failed", "canceled", "unknown_after_restart"})


def format_bridge_progress_reply(task: HubTask, *, progress_text: str | None = None, context_left_percent: int | None = None) -> str:
    return prefix_weixin_output(
        "running",
        format_duration_since(task.started_at or task.created_at),
        str(progress_text if progress_text is not None else task.progress_text).strip(),
        at=task.progress_at or "",
        context_left_percent=context_left_percent,
    )


def format_bridge_task_reply(task: HubTask, *, last_progress_text: str = "", context_left_percent: int | None = None) -> str:
    if task.status == "succeeded":
        output = task.output.strip() or "(empty)"
        body = "" if last_progress_text and normalize_message_for_dedupe(output) == normalize_message_for_dedupe(last_progress_text) else output
        return prefix_weixin_output(
            "done",
            format_duration_since(task.started_at or task.created_at, ended_at=task.finished_at),
            body,
            at=task.finished_at,
            context_left_percent=context_left_percent,
        )
    return f"任务 {task.status or 'failed'}：{(task.error or 'unknown error').strip()}"


class PollingTaskDeliveryController:
    def __init__(
        self,
        *,
        default_backend: str,
        task_timeout_seconds: int,
        get_task: Callable[[str], HubTask | None],
        send_reply: Callable[[dict[str, Any], str], None],
        send_typing_keepalive: Callable[[dict[str, Any], float], float],
        stop_typing: Callable[[dict[str, Any]], None],
        resolve_context_left_percent: Callable[[HubTask], int | None],
        get_pending_task: Callable[[str], Any | None],
        update_pending_progress: Callable[[str, int, str], None],
        forget_pending_task: Callable[[str], None],
        log: Callable[[str], None],
        sleep_seconds: float = 1.0,
    ) -> None:
        self.default_backend = default_backend
        self.task_timeout_seconds = task_timeout_seconds
        self.get_task = get_task
        self.send_reply = send_reply
        self.send_typing_keepalive = send_typing_keepalive
        self.stop_typing = stop_typing
        self.resolve_context_left_percent = resolve_context_left_percent
        self.get_pending_task = get_pending_task
        self.update_pending_progress = update_pending_progress
        self.forget_pending_task = forget_pending_task
        self.log = log
        self.sleep_seconds = sleep_seconds

    def wait_and_reply(self, reply_target: dict[str, Any], task_id: str) -> None:
        deadline = time.time() + max(60, int(self.task_timeout_seconds))
        latest_task: HubTask | None = None
        pending_task = self.get_pending_task(task_id)
        last_progress_seq = pending_task.last_progress_seq if pending_task is not None else 0
        last_sent_progress_text = pending_task.last_progress_text if pending_task is not None else ""
        typing_last_sent_at = 0.0
        while time.time() < deadline:
            latest_task = self.get_task(task_id) or latest_task
            if latest_task is not None:
                if latest_task.status in {"queued", "running"}:
                    typing_last_sent_at = self.send_typing_keepalive(reply_target, typing_last_sent_at)
                if latest_task.status in {"queued", "running"} and latest_task.progress_seq > last_progress_seq and latest_task.progress_text.strip():
                    progress_text = latest_task.progress_text.strip()
                    progress_delta = extract_progress_delta(last_sent_progress_text, progress_text)
                    if str(progress_delta or "").strip():
                        self.send_reply(
                            reply_target,
                            format_bridge_progress_reply(
                                latest_task,
                                progress_text=progress_delta,
                                context_left_percent=self.resolve_context_left_percent(latest_task),
                            ),
                        )
                        last_sent_progress_text = progress_text
                        self.update_pending_progress(task_id, latest_task.progress_seq, last_sent_progress_text)
                    last_progress_seq = latest_task.progress_seq
                    self.update_pending_progress(task_id, last_progress_seq, last_sent_progress_text)
                if latest_task.status in TERMINAL_TASK_STATUSES:
                    break
            time.sleep(self.sleep_seconds)
        self.stop_typing(reply_target)
        if latest_task is None:
            self.send_reply(reply_target, f"任务状态查询失败：{task_id}")
            return
        self.log(
            f"task terminal task_id={task_id} status={latest_task.status} "
            f"output_preview={latest_task.output[:80]!r} error_preview={latest_task.error[:80]!r}"
        )
        final_reply = format_bridge_task_reply(
            latest_task,
            last_progress_text=last_sent_progress_text,
            context_left_percent=self.resolve_context_left_percent(latest_task),
        )
        if final_reply:
            self.send_reply(reply_target, final_reply)
        if latest_task.status in TERMINAL_TASK_STATUSES:
            self.forget_pending_task(task_id)


class TaskUpdateDeliveryController:
    def __init__(
        self,
        *,
        send_progress: Callable[[Any, Any, HubTask, str], None],
        send_terminal: Callable[[Any, Any, HubTask], None],
        save_pending_task: Callable[[str], None],
        forget_pending_task: Callable[[str], None],
        send_typing_keepalive: Callable[[Any, float], float] | None = None,
        on_running: Callable[[Any, Any, HubTask], None] | None = None,
        should_send_progress: Callable[[str], bool] | None = None,
    ) -> None:
        self.send_typing_keepalive = send_typing_keepalive
        self.on_running = on_running
        self.send_progress = send_progress
        self.send_terminal = send_terminal
        self.save_pending_task = save_pending_task
        self.forget_pending_task = forget_pending_task
        self.should_send_progress = should_send_progress or (lambda progress_delta: bool(str(progress_delta or "").strip()))

    def handle_task_update(
        self,
        *,
        reply_target: dict[str, Any],
        task: HubTask,
        pending_task: Any,
        typing_last_sent_at: float = 0.0,
    ) -> tuple[float, bool]:
        next_typing_sent_at = typing_last_sent_at
        state_updated = False
        if task.status in {"queued", "running"} and self.send_typing_keepalive is not None:
            next_typing_sent_at = self.send_typing_keepalive(reply_target, typing_last_sent_at)
        last_status = str(getattr(pending_task, "last_status", "queued") or "queued")
        if task.status == "running" and last_status != "running":
            if self.on_running is not None:
                self.on_running(reply_target, pending_task, task)
            if hasattr(pending_task, "last_status"):
                setattr(pending_task, "last_status", "running")
            self.save_pending_task(task.id)
            state_updated = True
        last_progress_seq = int(getattr(pending_task, "last_progress_seq", 0) or 0)
        last_progress_text = str(getattr(pending_task, "last_progress_text", "") or "")
        if task.status in {"queued", "running"} and task.progress_seq > last_progress_seq and task.progress_text.strip():
            progress_text = task.progress_text.strip()
            progress_delta = extract_progress_delta(last_progress_text, progress_text)
            last_sent_progress_text = last_progress_text
            if self.should_send_progress(progress_delta):
                self.send_progress(reply_target, pending_task, task, progress_delta)
                last_sent_progress_text = progress_text
            setattr(pending_task, "last_progress_seq", int(task.progress_seq or 0))
            setattr(pending_task, "last_progress_text", last_sent_progress_text)
            self.save_pending_task(task.id)
            state_updated = True
        if task.status in TERMINAL_TASK_STATUSES:
            self.send_terminal(reply_target, pending_task, task)
            self.forget_pending_task(task.id)
            state_updated = True
        return next_typing_sent_at, state_updated
