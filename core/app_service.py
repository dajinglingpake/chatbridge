from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agent_backends import supported_backend_keys
from env_tools import run_shell_command
from local_ipc import create_request, wait_for_response
from runtime_stack import BRIDGE_CONVERSATIONS_PATH, emergency_stop, get_runtime_snapshot, restart_all, restart_bridge, start_all, stop_all

from core.accounts import activate_account
from bridge_config import APP_DIR, BridgeConfig
from core.weixin_notifier import broadcast_weixin_notice_by_kind


ActionRunner = Callable[[], list[str]]


@dataclass
class ServiceResult:
    ok: bool
    message: str


@dataclass
class AgentServiceResult:
    ok: bool
    message: str
    agent: dict[str, Any] | None = None


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
    result_message = " | ".join(runner())
    notice = broadcast_weixin_notice_by_kind("service", f"服务操作: {action}", result_message)
    return ServiceResult(ok=True, message=f"{result_message} | {notice.summary}")


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
        message = f"任务已入队：{task.get('id')}"
        notice = broadcast_weixin_notice_by_kind("task", "提交任务", message)
        return ServiceResult(ok=True, message=f"{message} | {notice.summary}")
    message = f"提交失败：{response.get('error') or 'unknown error'}"
    notice = broadcast_weixin_notice_by_kind("task", "提交任务", message)
    return ServiceResult(ok=False, message=f"{message} | {notice.summary}")


def switch_active_account(account_id: str, restart_if_running: bool = True) -> ServiceResult:
    cleaned_account_id = account_id.strip()
    if not cleaned_account_id:
        return ServiceResult(ok=False, message="切换失败：account_id 不能为空")
    snapshot = get_runtime_snapshot()
    pre_notice = broadcast_weixin_notice_by_kind("config", "切换微信账号", f"准备切换当前账号到: {cleaned_account_id}")
    activate_account(cleaned_account_id)
    if restart_if_running and (snapshot.hub_running or snapshot.bridge_running):
        messages = restart_all()
        return ServiceResult(ok=True, message=f"已切换当前账号：{cleaned_account_id} | {' | '.join(messages)} | {pre_notice.summary}")
    return ServiceResult(ok=True, message=f"已切换当前账号：{cleaned_account_id} | {pre_notice.summary}")


def run_repair_command(command: str, label: str = "") -> ServiceResult:
    cleaned = command.strip()
    if not cleaned:
        return ServiceResult(ok=False, message="修复失败：命令为空")
    code, output = run_shell_command(cleaned, APP_DIR)
    title = label or "修复命令"
    if code == 0:
        suffix = f" | {output}" if output else ""
        message = f"{title} 执行完成{suffix}"
        notice = broadcast_weixin_notice_by_kind("config", title, message)
        return ServiceResult(ok=True, message=f"{message} | {notice.summary}")
    suffix = f" | {output}" if output else ""
    message = f"{title} 执行失败，退出码 {code}{suffix}"
    notice = broadcast_weixin_notice_by_kind("config", title, message)
    return ServiceResult(ok=False, message=f"{message} | {notice.summary}")


def save_agent(
    agent_id: str,
    name: str,
    workdir: str,
    session_file: str,
    backend: str,
    model: str = "",
    prompt_prefix: str = "",
    enabled: bool = True,
) -> AgentServiceResult:
    cleaned_id = agent_id.strip()
    cleaned_name = name.strip()
    cleaned_workdir = workdir.strip()
    cleaned_session_file = session_file.strip()
    if not cleaned_id:
        return AgentServiceResult(ok=False, message="保存失败：agent_id 不能为空")
    if not cleaned_name:
        return AgentServiceResult(ok=False, message="保存失败：名称不能为空")
    if not cleaned_workdir:
        return AgentServiceResult(ok=False, message="保存失败：workdir 不能为空")
    if not cleaned_session_file:
        return AgentServiceResult(ok=False, message="保存失败：session_file 不能为空")

    request_id = create_request(
        "save_agent",
        {
            "id": cleaned_id,
            "name": cleaned_name,
            "workdir": cleaned_workdir,
            "session_file": cleaned_session_file,
            "backend": backend.strip(),
            "model": model.strip(),
            "prompt_prefix": prompt_prefix.strip(),
            "enabled": bool(enabled),
        },
    )
    try:
        response = wait_for_response(request_id, timeout_seconds=5)
    except TimeoutError:
        return AgentServiceResult(ok=False, message="保存失败：Hub 响应超时")

    if response.get("ok"):
        agent = response.get("agent") or {}
        message = f"已保存 Agent：{agent.get('name') or cleaned_id}"
        notice = broadcast_weixin_notice_by_kind("config", "保存 Agent", message)
        return AgentServiceResult(ok=True, message=f"{message} | {notice.summary}", agent=agent)
    return AgentServiceResult(ok=False, message=f"保存失败：{response.get('error') or 'unknown error'}")


def delete_agent(agent_id: str) -> ServiceResult:
    cleaned_id = agent_id.strip()
    if not cleaned_id:
        return ServiceResult(ok=False, message="删除失败：agent_id 不能为空")
    request_id = create_request("delete_agent", {"agent_id": cleaned_id})
    try:
        response = wait_for_response(request_id, timeout_seconds=5)
    except TimeoutError:
        return ServiceResult(ok=False, message="删除失败：Hub 响应超时")
    if response.get("ok"):
        message = f"已删除 Agent：{cleaned_id}"
        notice = broadcast_weixin_notice_by_kind("config", "删除 Agent", message)
        return ServiceResult(ok=True, message=f"{message} | {notice.summary}")
    return ServiceResult(ok=False, message=f"删除失败：{response.get('error') or 'unknown error'}")


def switch_bridge_agent(agent_id: str, restart_if_running: bool = True) -> ServiceResult:
    cleaned_id = agent_id.strip()
    if not cleaned_id:
        return ServiceResult(ok=False, message="切换失败：agent_id 不能为空")

    request_id = create_request("state", {})
    try:
        response = wait_for_response(request_id, timeout_seconds=5)
    except TimeoutError:
        response = {"ok": False}
    if response.get("ok"):
        agents = response.get("agents") or []
        known_ids = {str(item.get("id") or "") for item in agents}
        if known_ids and cleaned_id not in known_ids:
            return ServiceResult(ok=False, message=f"切换失败：未找到 Agent {cleaned_id}")

    config = BridgeConfig.load()
    config.set_backend_agent(cleaned_id)
    config.save()

    snapshot = get_runtime_snapshot()
    if restart_if_running and snapshot.bridge_running:
        messages = restart_bridge()
        message = f"已切换微信桥默认 Agent：{cleaned_id} | {' | '.join(messages)}"
        notice = broadcast_weixin_notice_by_kind("config", "切换微信桥默认 Agent", message, config=BridgeConfig.load())
        return ServiceResult(ok=True, message=f"{message} | {notice.summary}")
    message = f"已切换微信桥默认 Agent：{cleaned_id}"
    notice = broadcast_weixin_notice_by_kind("config", "切换微信桥默认 Agent", message, config=BridgeConfig.load())
    return ServiceResult(ok=True, message=f"{message} | {notice.summary}")


def set_weixin_notice_enabled(service_enabled: bool, config_enabled: bool, task_enabled: bool) -> ServiceResult:
    config = BridgeConfig.load()
    config.service_notice_enabled = bool(service_enabled)
    config.config_notice_enabled = bool(config_enabled)
    config.task_notice_enabled = bool(task_enabled)
    config.save()
    return ServiceResult(
        ok=True,
        message=(
            f"已更新微信系统通知：服务生命周期={'开' if config.service_notice_enabled else '关'}，"
            f"配置变更={'开' if config.config_notice_enabled else '关'}，"
            f"任务通知={'开' if config.task_notice_enabled else '关'}"
        ),
    )


def switch_weixin_session_backend(sender_id: str, backend: str) -> ServiceResult:
    cleaned_sender_id = sender_id.strip()
    cleaned_backend = backend.strip().lower()
    if not cleaned_sender_id:
        return ServiceResult(ok=False, message="切换失败：sender_id 不能为空")
    if cleaned_backend not in set(supported_backend_keys()):
        return ServiceResult(ok=False, message=f"切换失败：不支持的后端 {cleaned_backend}")

    payload = _read_conversations_file(BRIDGE_CONVERSATIONS_PATH)
    binding = payload.get(cleaned_sender_id)
    if not isinstance(binding, dict):
        return ServiceResult(ok=False, message=f"切换失败：未找到发送方 {cleaned_sender_id}")
    current_session = str(binding.get("current_session") or "default")
    sessions = binding.get("sessions") or {}
    if not isinstance(sessions, dict):
        return ServiceResult(ok=False, message=f"切换失败：发送方 {cleaned_sender_id} 的会话状态损坏")
    current_meta = sessions.get(current_session)
    if not isinstance(current_meta, dict):
        return ServiceResult(ok=False, message=f"切换失败：未找到当前会话 {current_session}")
    current_meta["backend"] = cleaned_backend
    sessions[current_session] = current_meta
    binding["sessions"] = sessions
    payload[cleaned_sender_id] = binding
    BRIDGE_CONVERSATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    BRIDGE_CONVERSATIONS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    snapshot = get_runtime_snapshot()
    message = f"已切换发送方 {cleaned_sender_id} 的当前会话后端为 {cleaned_backend}"
    if snapshot.bridge_running:
        restart_messages = restart_bridge()
        message = f"{message} | {' | '.join(restart_messages)}"
    notice = broadcast_weixin_notice_by_kind("config", "切换微信会话后端", message)
    return ServiceResult(ok=True, message=f"{message} | {notice.summary}")


def reset_weixin_conversation(sender_id: str) -> ServiceResult:
    cleaned_sender_id = sender_id.strip()
    if not cleaned_sender_id:
        return ServiceResult(ok=False, message="重置失败：sender_id 不能为空")

    payload = _read_conversations_file(BRIDGE_CONVERSATIONS_PATH)
    if cleaned_sender_id not in payload:
        return ServiceResult(ok=False, message=f"重置失败：未找到发送方 {cleaned_sender_id}")

    payload.pop(cleaned_sender_id, None)
    BRIDGE_CONVERSATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    BRIDGE_CONVERSATIONS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    snapshot = get_runtime_snapshot()
    message = f"已重置发送方 {cleaned_sender_id} 的微信会话状态"
    if snapshot.bridge_running:
        restart_messages = restart_bridge()
        message = f"{message} | {' | '.join(restart_messages)}"
    notice = broadcast_weixin_notice_by_kind("config", "重置微信会话", message)
    return ServiceResult(ok=True, message=f"{message} | {notice.summary}")


def _read_conversations_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
