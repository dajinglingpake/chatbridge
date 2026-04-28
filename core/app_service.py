from __future__ import annotations

import argparse
import json
import os
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
import subprocess
import sys
import time

from agent_backends import supported_backend_keys
from env_tools import run_shell_command
from local_ipc import create_request, wait_for_response
from runtime_stack import BRIDGE_CONVERSATIONS_PATH, emergency_stop, get_runtime_snapshot, restart_all, restart_bridge, start_all, stop_all, stop_external_agent_process

from core.accounts import account_conversation_path, activate_account
from bridge_config import APP_DIR, BridgeConfig, normalize_backend
from core.json_store import load_json, save_json
from core.platform_compat import creationflags
from core.runtime_paths import APP_SERVICE_ERR_LOG, APP_SERVICE_OUT_LOG, LOG_DIR, SERVICE_ACTION_LOG_PATH, SERVICE_ACTION_STATE_PATH, STATE_DIR
from core.state_models import HubAgentSnapshot, HubTask, JsonObject, WeixinConversationBinding
from core.weixin_notifier import broadcast_weixin_notice_by_kind


ActionRunner = Callable[[], list[str]]
STOP_NOTICE_DRAIN_SECONDS = 1.0


@dataclass
class ServiceResult:
    ok: bool
    message: str


@dataclass
class AgentServiceResult:
    ok: bool
    message: str
    agent: HubAgentSnapshot | None = None


def _append_action_log(event: str, **payload: object) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    record = {"at": _state_now(), "event": event, **payload}
    with SERVICE_ACTION_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _write_action_state(**payload: object) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    save_json(SERVICE_ACTION_STATE_PATH, {"updated_at": _state_now(), **payload})


def _parse_hub_task(raw: object) -> HubTask | None:
    return HubTask.from_dict(raw, default_backend="")


def _parse_hub_agent(raw: object) -> HubAgentSnapshot | None:
    return HubAgentSnapshot.from_dict(raw, now=_state_now())


def _parse_hub_agents(raw: object) -> list[HubAgentSnapshot]:
    if not isinstance(raw, list):
        return []
    return [agent for item in raw if (agent := _parse_hub_agent(item)) is not None]


def run_named_action(action: str) -> ServiceResult:
    actions: dict[str, ActionRunner] = {
        "start": start_all,
        "stop": stop_all,
        "restart": restart_all,
        "restart-bridge": restart_bridge,
        "emergency-stop": emergency_stop,
    }
    runner = actions.get(action)
    if runner is None:
        return ServiceResult(ok=False, message=f"未知操作：{action}")
    pre_notice = _broadcast_pre_stop_notice(action) if action in {"stop", "emergency-stop"} else None
    result_message = " | ".join(runner())
    if pre_notice is not None:
        return ServiceResult(ok=True, message=f"{result_message} | {pre_notice.summary}")
    notice = broadcast_weixin_notice_by_kind("service", f"服务操作: {action}", result_message)
    return ServiceResult(ok=True, message=f"{result_message} | {notice.summary}")


def _broadcast_pre_stop_notice(action: str):
    snapshot = get_runtime_snapshot()
    detail = (
        f"即将执行服务停止操作: {action}\n"
        f"Hub PID: {snapshot.hub_pid or '-'}\n"
        f"Bridge PID: {snapshot.bridge_pid or '-'}"
    )
    notice = broadcast_weixin_notice_by_kind("service", f"服务操作: {action}", detail)
    if notice.recipient_count > 0 and notice.error != "disabled":
        time.sleep(STOP_NOTICE_DRAIN_SECONDS)
    return notice


def schedule_named_action(action: str, *, delay_seconds: float = 1.0) -> ServiceResult:
    cleaned_action = action.strip()
    if cleaned_action not in {"start", "stop", "restart", "restart-bridge", "emergency-stop"}:
        return ServiceResult(ok=False, message=f"未知操作：{cleaned_action or action}")

    safe_delay = max(0.0, float(delay_seconds))
    request_id = f"svc-{uuid.uuid4().hex[:12]}"
    snapshot = get_runtime_snapshot()
    command = [
        sys.executable,
        "-m",
        "core.app_service",
        "--spawn-runner",
        cleaned_action,
        "--request-id",
        request_id,
        "--delay-seconds",
        f"{safe_delay:.2f}",
    ]
    APP_SERVICE_OUT_LOG.parent.mkdir(parents=True, exist_ok=True)
    _write_action_state(
        request_id=request_id,
        action=cleaned_action,
        status="scheduled",
        delay_seconds=safe_delay,
        scheduler_pid=os.getpid(),
        scheduler_python=sys.executable,
        hub_pid=snapshot.hub_pid,
        bridge_pid=snapshot.bridge_pid,
    )
    _append_action_log(
        "scheduled",
        request_id=request_id,
        action=cleaned_action,
        delay_seconds=safe_delay,
        scheduler_pid=os.getpid(),
        scheduler_python=sys.executable,
        hub_pid=snapshot.hub_pid,
        bridge_pid=snapshot.bridge_pid,
    )
    with APP_SERVICE_OUT_LOG.open("ab") as out_handle, APP_SERVICE_ERR_LOG.open("ab") as err_handle:
        proc = subprocess.Popen(
            command,
            cwd=str(APP_DIR),
            stdout=out_handle,
            stderr=err_handle,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags(),
            start_new_session=True,
        )
    _write_action_state(
        request_id=request_id,
        action=cleaned_action,
        status="launcher_spawned",
        delay_seconds=safe_delay,
        scheduler_pid=os.getpid(),
        launcher_pid=proc.pid,
        scheduler_python=sys.executable,
        hub_pid=snapshot.hub_pid,
        bridge_pid=snapshot.bridge_pid,
    )
    _append_action_log(
        "launcher_spawned",
        request_id=request_id,
        action=cleaned_action,
        launcher_pid=proc.pid,
    )
    return ServiceResult(ok=True, message=f"已安排在 {safe_delay:.2f} 秒后执行服务操作：{cleaned_action}（请求 {request_id}）")


def spawn_named_action_runner(action: str, request_id: str, delay_seconds: float) -> int:
    cleaned_action = action.strip()
    safe_delay = max(0.0, float(delay_seconds))
    command = [
        sys.executable,
        "-m",
        "core.app_service",
        "--run-named-action",
        cleaned_action,
        "--request-id",
        request_id,
        "--delay-seconds",
        f"{safe_delay:.2f}",
    ]
    APP_SERVICE_OUT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with APP_SERVICE_OUT_LOG.open("ab") as out_handle, APP_SERVICE_ERR_LOG.open("ab") as err_handle:
        proc = subprocess.Popen(
            command,
            cwd=str(APP_DIR),
            stdout=out_handle,
            stderr=err_handle,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags(),
            start_new_session=True,
        )
    _write_action_state(
        request_id=request_id,
        action=action,
        status="child_spawned",
        delay_seconds=delay_seconds,
        launcher_pid=os.getpid(),
        child_pid=proc.pid,
        scheduler_python=sys.executable,
    )
    _append_action_log(
        "child_spawned",
        request_id=request_id,
        action=action,
        launcher_pid=os.getpid(),
        child_pid=proc.pid,
    )
    return proc.pid


def submit_hub_task(
    agent_id: str,
    prompt: str,
    session_name: str = "",
    backend: str = "",
    *,
    source: str = "web",
    sender_id: str = "",
    workdir: str = "",
    model: str = "",
) -> ServiceResult:
    cleaned_prompt = prompt.strip()
    if not cleaned_prompt:
        return ServiceResult(ok=False, message="提交失败：prompt 不能为空")

    request_id = create_request(
        "submit_task",
        {
            "agent_id": agent_id.strip() or "main",
            "prompt": cleaned_prompt,
            "source": source.strip() or "web",
            "sender_id": sender_id.strip(),
            "session_name": session_name.strip(),
            "backend": backend.strip(),
            "workdir": workdir.strip(),
            "model": model.strip(),
        },
    )
    try:
        response = wait_for_response(request_id, timeout_seconds=5)
    except TimeoutError:
        return ServiceResult(ok=False, message="提交失败：Hub 响应超时")

    if response.ok:
        task = _parse_hub_task(response.payload.get("task"))
        task_id = task.id if task is not None else ""
        message = f"任务已入队：{task_id or request_id}"
        notice = broadcast_weixin_notice_by_kind("task", "提交任务", message)
        return ServiceResult(ok=True, message=f"{message} | {notice.summary}")
    message = f"提交失败：{response.error or 'unknown error'}"
    notice = broadcast_weixin_notice_by_kind("task", "提交任务", message)
    return ServiceResult(ok=False, message=f"{message} | {notice.summary}")


def switch_active_account(account_id: str, restart_if_running: bool = True) -> ServiceResult:
    cleaned_account_id = account_id.strip()
    if not cleaned_account_id:
        return ServiceResult(ok=False, message="切换失败：account_id 不能为空")
    config = BridgeConfig.load()
    if not any(account.account_id == cleaned_account_id and account.is_usable for account in config.accounts):
        return ServiceResult(ok=False, message=f"切换失败：账号不存在或文件不完整：{cleaned_account_id}")
    snapshot = get_runtime_snapshot()
    activate_account(cleaned_account_id, config=config)
    if not restart_if_running:
        return ServiceResult(ok=True, message=f"已切换当前账号：{cleaned_account_id} | Bridge 将在下一次轮询自动使用新账号（无需重启）")
    if restart_if_running and (snapshot.hub_running or snapshot.bridge_running):
        messages = restart_all()
        return ServiceResult(ok=True, message=f"已切换当前账号：{cleaned_account_id} | {' | '.join(messages)}")
    return ServiceResult(ok=True, message=f"已切换当前账号：{cleaned_account_id}")


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

    if response.ok:
        agent_snapshot = _parse_hub_agent(response.payload.get("agent"))
        agent_name = agent_snapshot.name if agent_snapshot is not None else cleaned_id
        message = f"已保存 Agent：{agent_name}"
        notice = broadcast_weixin_notice_by_kind("config", "保存 Agent", message)
        return AgentServiceResult(ok=True, message=f"{message} | {notice.summary}", agent=agent_snapshot)
    return AgentServiceResult(ok=False, message=f"保存失败：{response.error or 'unknown error'}")


def delete_agent(agent_id: str) -> ServiceResult:
    cleaned_id = agent_id.strip()
    if not cleaned_id:
        return ServiceResult(ok=False, message="删除失败：agent_id 不能为空")
    request_id = create_request("delete_agent", {"agent_id": cleaned_id})
    try:
        response = wait_for_response(request_id, timeout_seconds=5)
    except TimeoutError:
        return ServiceResult(ok=False, message="删除失败：Hub 响应超时")
    if response.ok:
        message = f"已删除 Agent：{cleaned_id}"
        notice = broadcast_weixin_notice_by_kind("config", "删除 Agent", message)
        return ServiceResult(ok=True, message=f"{message} | {notice.summary}")
    return ServiceResult(ok=False, message=f"删除失败：{response.error or 'unknown error'}")


def switch_bridge_agent(agent_id: str, restart_if_running: bool = True) -> ServiceResult:
    cleaned_id = agent_id.strip()
    if not cleaned_id:
        return ServiceResult(ok=False, message="切换失败：agent_id 不能为空")

    request_id = create_request("state", {})
    try:
        response = wait_for_response(request_id, timeout_seconds=5)
    except TimeoutError:
        response = None
    if response and response.ok:
        agents = _parse_hub_agents(response.payload.get("agents"))
        known_ids = {agent.id for agent in agents}
        if known_ids and cleaned_id not in known_ids:
            return ServiceResult(ok=False, message=f"切换失败：未找到 Agent {cleaned_id}")

    config = BridgeConfig.load()
    config.set_backend_agent(cleaned_id)
    config.save()

    snapshot = get_runtime_snapshot()
    if restart_if_running and snapshot.bridge_running:
        messages = restart_bridge()
        message = f"已切换微信桥默认 Agent：{cleaned_id} | {' | '.join(messages)}"
        notice = broadcast_weixin_notice_by_kind("config", "切换微信桥默认 Agent", message, config=config)
        return ServiceResult(ok=True, message=f"{message} | {notice.summary}")
    message = f"已切换微信桥默认 Agent：{cleaned_id}"
    notice = broadcast_weixin_notice_by_kind("config", "切换微信桥默认 Agent", message, config=config)
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


def terminate_external_agent(pid: int) -> ServiceResult:
    message = stop_external_agent_process(int(pid))
    ok = message.startswith("已结束")
    notice = broadcast_weixin_notice_by_kind("service", "结束外部 Agent 进程", message)
    return ServiceResult(ok=ok, message=f"{message} | {notice.summary}")


def switch_weixin_session_backend(sender_id: str, backend: str) -> ServiceResult:
    cleaned_sender_id = sender_id.strip()
    cleaned_backend = backend.strip().lower()
    if not cleaned_sender_id:
        return ServiceResult(ok=False, message="切换失败：sender_id 不能为空")
    if cleaned_backend not in set(supported_backend_keys()):
        return ServiceResult(ok=False, message=f"切换失败：不支持的后端 {cleaned_backend}")

    config = BridgeConfig.load()
    conversation_path = _conversation_path_for_config(BRIDGE_CONVERSATIONS_PATH, config)
    bindings = _read_conversation_bindings(conversation_path, config)
    binding = bindings.get(cleaned_sender_id)
    if binding is None:
        return ServiceResult(ok=False, message=f"切换失败：未找到发送方 {cleaned_sender_id}")
    _, current_meta = binding.get_current_session(
        default_backend=config.default_backend,
        now=_state_now(),
        normalize_backend=normalize_backend,
    )
    current_meta.backend = cleaned_backend
    bindings[cleaned_sender_id] = binding
    _save_conversation_bindings(conversation_path, bindings)

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

    config = BridgeConfig.load()
    conversation_path = _conversation_path_for_config(BRIDGE_CONVERSATIONS_PATH, config)
    bindings = _read_conversation_bindings(conversation_path, config)
    if cleaned_sender_id not in bindings:
        return ServiceResult(ok=False, message=f"重置失败：未找到发送方 {cleaned_sender_id}")

    bindings.pop(cleaned_sender_id, None)
    _save_conversation_bindings(conversation_path, bindings)

    snapshot = get_runtime_snapshot()
    message = f"已重置发送方 {cleaned_sender_id} 的微信会话状态"
    if snapshot.bridge_running:
        restart_messages = restart_bridge()
        message = f"{message} | {' | '.join(restart_messages)}"
    notice = broadcast_weixin_notice_by_kind("config", "重置微信会话", message)
    return ServiceResult(ok=True, message=f"{message} | {notice.summary}")


def _read_conversations_file(path: Path) -> JsonObject:
    data = load_json(path, {}, expect_type=dict)
    return data if isinstance(data, dict) else {}


def _conversation_path_for_config(path: Path, config: BridgeConfig) -> Path:
    return account_conversation_path(path, config.active_account_id, config.account_file)


def _read_conversation_bindings(path: Path, config: BridgeConfig | None = None) -> dict[str, WeixinConversationBinding]:
    payload = _read_conversations_file(path)
    resolved_config = config or BridgeConfig.load()
    bindings: dict[str, WeixinConversationBinding] = {}
    for sender_id, raw_binding in payload.items():
        cleaned_sender_id = str(sender_id or "").strip()
        if not cleaned_sender_id:
            continue
        bindings[cleaned_sender_id] = WeixinConversationBinding.from_dict(
            raw_binding,
            default_backend=resolved_config.default_backend,
            now=_state_now(),
            normalize_backend=normalize_backend,
        )
    return bindings


def _save_conversation_bindings(path: Path, bindings: dict[str, WeixinConversationBinding]) -> None:
    save_json(
        path,
        {sender_id: binding.to_dict() for sender_id, binding in bindings.items()},
    )


def _state_now() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run app service actions.")
    parser.add_argument("--spawn-runner", default="", help="Spawn a detached action runner and exit.")
    parser.add_argument("--run-named-action", default="", help="Execute a named runtime action.")
    parser.add_argument("--request-id", default="", help="Correlation ID for an async scheduled action.")
    parser.add_argument("--delay-seconds", type=float, default=0.0, help="Optional delay before the action runs.")
    args = parser.parse_args(argv)

    spawn_action = str(args.spawn_runner or "").strip()
    if spawn_action:
        request_id = str(args.request_id or "").strip() or f"svc-{uuid.uuid4().hex[:12]}"
        spawn_named_action_runner(spawn_action, request_id, max(0.0, float(args.delay_seconds or 0.0)))
        return 0

    action = str(args.run_named_action or "").strip()
    if not action:
        parser.print_help()
        return 1

    delay_seconds = max(0.0, float(args.delay_seconds or 0.0))
    request_id = str(args.request_id or "").strip() or f"svc-{uuid.uuid4().hex[:12]}"
    _write_action_state(
        request_id=request_id,
        action=action,
        status="child_started",
        child_pid=os.getpid(),
        delay_seconds=delay_seconds,
    )
    _append_action_log(
        "child_started",
        request_id=request_id,
        action=action,
        child_pid=os.getpid(),
        delay_seconds=delay_seconds,
    )
    try:
        if delay_seconds > 0:
            time.sleep(delay_seconds)
        before = get_runtime_snapshot()
        _write_action_state(
            request_id=request_id,
            action=action,
            status="running",
            child_pid=os.getpid(),
            delay_seconds=delay_seconds,
            hub_pid_before=before.hub_pid,
            bridge_pid_before=before.bridge_pid,
        )
        _append_action_log(
            "running",
            request_id=request_id,
            action=action,
            child_pid=os.getpid(),
            hub_pid_before=before.hub_pid,
            bridge_pid_before=before.bridge_pid,
        )
        result = run_named_action(action)
        after = get_runtime_snapshot()
        status = "succeeded" if result.ok else "failed"
        _write_action_state(
            request_id=request_id,
            action=action,
            status=status,
            child_pid=os.getpid(),
            delay_seconds=delay_seconds,
            result_message=result.message,
            hub_pid_before=before.hub_pid,
            bridge_pid_before=before.bridge_pid,
            hub_pid_after=after.hub_pid,
            bridge_pid_after=after.bridge_pid,
        )
        _append_action_log(
            status,
            request_id=request_id,
            action=action,
            child_pid=os.getpid(),
            result_message=result.message,
            hub_pid_before=before.hub_pid,
            bridge_pid_before=before.bridge_pid,
            hub_pid_after=after.hub_pid,
            bridge_pid_after=after.bridge_pid,
        )
        return 0 if result.ok else 1
    except Exception as exc:  # noqa: BLE001
        _write_action_state(
            request_id=request_id,
            action=action,
            status="crashed",
            child_pid=os.getpid(),
            error=str(exc),
            traceback=traceback.format_exc(),
        )
        _append_action_log(
            "crashed",
            request_id=request_id,
            action=action,
            child_pid=os.getpid(),
            error=str(exc),
            traceback=traceback.format_exc(),
        )
        raise


if __name__ == "__main__":
    raise SystemExit(_main())
