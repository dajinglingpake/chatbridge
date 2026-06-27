from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from core.state_models import HubTask

MAX_RESTART_RESUME_ATTEMPTS = 2

@dataclass(frozen=True)
class RestartResumePlan:
    should_resume: bool
    prompt: str = ""
    notice: str = ""
    reason: str = ""
    next_attempt: int = 0

def build_restart_resume_plan(
    task: HubTask,
    pending_task: Any,
    *,
    translate: Callable[..., str],
    max_attempts: int = MAX_RESTART_RESUME_ATTEMPTS,
) -> RestartResumePlan:
    if str(task.status or "").strip() != "unknown_after_restart":
        return RestartResumePlan(False, reason="status_not_restart_unknown")

    attempts = int(getattr(pending_task, "restart_resume_count", 0) or 0)
    if attempts >= max_attempts:
        return RestartResumePlan(False, reason="max_attempts_reached")

    original_prompt = _resolve_original_prompt(task, pending_task)
    if not original_prompt:
        return RestartResumePlan(False, reason="missing_original_prompt")

    next_attempt = attempts + 1
    notice = _translate(
        translate,
        "bridge.task.restart_resume.notice",
        "Hub 已重启，上一轮任务被中断。正在自动续跑原任务。\n原任务: {task_id}\n续跑次数: {attempt}/{max_attempts}",
        task_id=task.id,
        attempt=next_attempt,
        max_attempts=max_attempts,
    )
    prompt = _translate(
        translate,
        "bridge.task.restart_resume.prompt",
        "系统提示：上一轮 Agent 任务在 Hub 重启时被中断，现在正在自动续跑。\n"
        "请继续完成上一轮用户问题，不要只解释重启；如果之前可能已经执行过部分操作，请先检查当前项目状态，避免重复执行有副作用的步骤。\n\n"
        "上一轮用户问题：\n{prompt}",
        prompt=original_prompt,
    )
    return RestartResumePlan(True, prompt=prompt, notice=notice, next_attempt=next_attempt)

def _resolve_original_prompt(task: HubTask, pending_task: Any) -> str:
    base_prompt = str(getattr(pending_task, "interrupt_base_prompt", "") or "").strip()
    interrupt_messages = [
        str(item).strip()
        for item in getattr(pending_task, "interrupt_messages", []) or []
        if str(item or "").strip()
    ]
    if base_prompt or interrupt_messages:
        parts: list[str] = []
        if base_prompt:
            parts.append(base_prompt)
        for message in reversed(interrupt_messages):
            parts.append(f"用户补充：{message}")
        return "\n\n".join(parts).strip()
    return str(task.prompt or "").strip()

def _translate(translate: Callable[..., str], key: str, fallback: str, **kwargs: object) -> str:
    value = str(translate(key, **kwargs) or "")
    return fallback.format(**kwargs) if value == key or not value.strip() else value
