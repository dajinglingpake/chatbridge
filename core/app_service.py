from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from codex_wechat_ipc import create_request, wait_for_response
from codex_wechat_runtime import emergency_stop, restart_all, start_all, stop_all

from core.accounts import activate_account


ActionRunner = Callable[[], list[str]]


@dataclass
class ServiceResult:
    ok: bool
    message: str


def run_named_action(action: str) -> ServiceResult:
    actions: dict[str, ActionRunner] = {
        "start": start_all,
        "stop": stop_all,
        "restart": restart_all,
        "emergency-stop": emergency_stop,
    }
    runner = actions.get(action)
    if runner is None:
        return ServiceResult(ok=False, message=f"未知操作：{action}")
    return ServiceResult(ok=True, message=" | ".join(runner()))


def submit_hub_task(agent_id: str, prompt: str, session_name: str = "", backend: str = "") -> ServiceResult:
    cleaned_prompt = prompt.strip()
    if not cleaned_prompt:
        return ServiceResult(ok=False, message="提交失败：prompt 不能为空")

    request_id = create_request(
        "submit_task",
        {
            "agent_id": agent_id.strip() or "main",
            "prompt": cleaned_prompt,
            "source": "web",
            "session_name": session_name.strip(),
            "backend": backend.strip(),
        },
    )
    try:
        response = wait_for_response(request_id, timeout_seconds=5)
    except TimeoutError:
        return ServiceResult(ok=False, message="提交失败：Hub 响应超时")

    if response.get("ok"):
        task = response.get("task") or {}
        return ServiceResult(ok=True, message=f"任务已入队：{task.get('id')}")
    return ServiceResult(ok=False, message=f"提交失败：{response.get('error') or 'unknown error'}")


def switch_active_account(account_id: str) -> ServiceResult:
    cleaned_account_id = account_id.strip()
    if not cleaned_account_id:
        return ServiceResult(ok=False, message="切换失败：account_id 不能为空")
    activate_account(cleaned_account_id)
    return ServiceResult(ok=True, message=f"已切换当前账号：{cleaned_account_id}")
