from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Callable

from core.mcp_service import (
    ManagerActionResult,
    delegate_task,
    enter_control_mode,
    exit_control_mode,
    get_command_catalog,
    get_control_mode_state,
    get_management_snapshot,
    get_manager_guide,
    get_task,
    list_agents,
    run_sender_command,
    start_agent_session,
)
from core.state_models import JsonObject


SERVER_NAME = "chatbridge-manager"
SERVER_VERSION = "0.1.0"
LATEST_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = {
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: JsonObject
    handler: Callable[[JsonObject], ManagerActionResult]

    def to_mcp_dict(self) -> JsonObject:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


def _tool_result_text(result: ManagerActionResult) -> str:
    if not result.data:
        return result.summary
    return f"{result.summary}\n\n{json.dumps(result.data, ensure_ascii=False, indent=2, sort_keys=True)}"


def _jsonrpc_result(message_id: object, result: JsonObject) -> JsonObject:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _jsonrpc_error(message_id: object, code: int, message: str) -> JsonObject:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def _args_as_dict(raw: object) -> JsonObject:
    return raw if isinstance(raw, dict) else {}


def _build_tool_specs() -> dict[str, ToolSpec]:
    def no_args(handler: Callable[[], ManagerActionResult]) -> Callable[[JsonObject], ManagerActionResult]:
        return lambda _args: handler()

    return {
        "get_manager_guide": ToolSpec(
            name="get_manager_guide",
            description="返回 ChatBridge 管理助手的工作规则，包括进入/退出管理模式、控制平面隔离原则以及推荐操作流程。",
            input_schema={"type": "object", "properties": {}},
            handler=no_args(get_manager_guide),
        ),
        "get_control_mode_state": ToolSpec(
            name="get_control_mode_state",
            description="查看当前是否已进入管理模式。只有进入管理模式后，才能执行目标发送方命令、启动新 Agent 会话或委派任务。",
            input_schema={"type": "object", "properties": {}},
            handler=no_args(get_control_mode_state),
        ),
        "enter_control_mode": ToolSpec(
            name="enter_control_mode",
            description="显式进入 ChatBridge 管理模式。进入后才允许执行会修改状态或触发其他 Agent 的操作。",
            input_schema={
                "type": "object",
                "properties": {
                    "note": {"type": "string", "description": "本次进入管理模式的备注，可选。"},
                },
            },
            handler=lambda args: enter_control_mode(str(args.get("note") or "")),
        ),
        "exit_control_mode": ToolSpec(
            name="exit_control_mode",
            description="显式退出 ChatBridge 管理模式。退出后只保留只读查询能力。",
            input_schema={"type": "object", "properties": {}},
            handler=no_args(exit_control_mode),
        ),
        "get_command_catalog": ToolSpec(
            name="get_command_catalog",
            description="返回当前桥接层命令清单及其说明，便于管理助手教用户使用或在需要时退回桥命令代理。",
            input_schema={"type": "object", "properties": {}},
            handler=no_args(get_command_catalog),
        ),
        "get_management_snapshot": ToolSpec(
            name="get_management_snapshot",
            description="获取 ChatBridge 总览，或按 target_sender_id 获取某个发送方的当前会话、模型、工程目录和历史摘要。该工具是只读的。",
            input_schema={
                "type": "object",
                "properties": {
                    "target_sender_id": {"type": "string", "description": "目标发送方 ID；为空时返回全局总览。"},
                },
            },
            handler=lambda args: get_management_snapshot(str(args.get("target_sender_id") or "")),
        ),
        "list_agents": ToolSpec(
            name="list_agents",
            description="列出当前 Agent Hub 中所有 Agent 的后端、模型、工作目录和运行态摘要。",
            input_schema={"type": "object", "properties": {}},
            handler=no_args(list_agents),
        ),
        "get_task": ToolSpec(
            name="get_task",
            description="按 task_id 查询单个任务详情，包括输入摘要、结果摘要和所属 Agent/会话。",
            input_schema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "目标任务 ID。"},
                },
                "required": ["task_id"],
            },
            handler=lambda args: get_task(str(args.get("task_id") or "")),
        ),
        "run_sender_command": ToolSpec(
            name="run_sender_command",
            description="对目标发送方执行桥接层 slash 命令。该工具要求先进入管理模式，并且显式提供 target_sender_id，不会隐式切换管理助手自己的上下文。",
            input_schema={
                "type": "object",
                "properties": {
                    "target_sender_id": {"type": "string", "description": "目标发送方 ID。"},
                    "command": {"type": "string", "description": "以 / 开头的桥接层命令，例如 /status 或 /use deep-dive。"},
                },
                "required": ["target_sender_id", "command"],
            },
            handler=lambda args: run_sender_command(
                str(args.get("target_sender_id") or ""),
                str(args.get("command") or ""),
            ),
        ),
        "start_agent_session": ToolSpec(
            name="start_agent_session",
            description="为指定 Agent 显式启动一个全新的会话实例，并发送首条指令。该工具要求先进入管理模式；如果 session_name 已存在，会拒绝执行。",
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "目标 Agent ID。"},
                    "session_name": {"type": "string", "description": "新的 Agent 会话名，必须是未占用的新名字。"},
                    "prompt": {"type": "string", "description": "启动新会话时发送的首条指令。"},
                    "backend": {"type": "string", "description": "可选，会话后端覆盖。"},
                    "target_sender_id": {"type": "string", "description": "可选，任务归属的发送方 ID。"},
                    "workdir": {"type": "string", "description": "可选，会话工作目录覆盖。"},
                    "model": {"type": "string", "description": "可选，会话模型覆盖。"},
                },
                "required": ["agent_id", "session_name", "prompt"],
            },
            handler=lambda args: start_agent_session(
                str(args.get("agent_id") or ""),
                str(args.get("session_name") or ""),
                str(args.get("prompt") or ""),
                backend=str(args.get("backend") or ""),
                target_sender_id=str(args.get("target_sender_id") or ""),
                workdir=str(args.get("workdir") or ""),
                model=str(args.get("model") or ""),
            ),
        ),
        "delegate_task": ToolSpec(
            name="delegate_task",
            description="向指定 Agent 委派一条新指令。该工具要求先进入管理模式，不会隐式改变管理助手自己的控制上下文。",
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "目标 Agent ID。"},
                    "prompt": {"type": "string", "description": "要委派给目标 Agent 的指令。"},
                    "session_name": {"type": "string", "description": "可选，目标 Agent 会话名。"},
                    "backend": {"type": "string", "description": "可选，会话后端覆盖。"},
                    "target_sender_id": {"type": "string", "description": "可选，任务归属的发送方 ID。"},
                    "workdir": {"type": "string", "description": "可选，会话工作目录覆盖。"},
                    "model": {"type": "string", "description": "可选，会话模型覆盖。"},
                },
                "required": ["agent_id", "prompt"],
            },
            handler=lambda args: delegate_task(
                str(args.get("agent_id") or ""),
                str(args.get("prompt") or ""),
                session_name=str(args.get("session_name") or ""),
                backend=str(args.get("backend") or ""),
                target_sender_id=str(args.get("target_sender_id") or ""),
                workdir=str(args.get("workdir") or ""),
                model=str(args.get("model") or ""),
            ),
        ),
    }


TOOL_SPECS = _build_tool_specs()


def handle_request(message: JsonObject) -> JsonObject | None:
    message_id = message.get("id")
    method = str(message.get("method") or "")
    params = _args_as_dict(message.get("params"))

    if method == "initialize":
        requested_version = str(params.get("protocolVersion") or "").strip()
        protocol_version = requested_version if requested_version in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        return _jsonrpc_result(
            message_id,
            {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "This server exposes ChatBridge management tools. "
                    "Use enter_control_mode before mutating sender state, starting new agent sessions, or delegating tasks."
                ),
            },
        )
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return _jsonrpc_result(message_id, {})
    if method == "tools/list":
        return _jsonrpc_result(message_id, {"tools": [tool.to_mcp_dict() for tool in TOOL_SPECS.values()]})
    if method == "tools/call":
        tool_name = str(params.get("name") or "")
        tool = TOOL_SPECS.get(tool_name)
        if tool is None:
            return _jsonrpc_error(message_id, -32602, f"Unknown tool: {tool_name}")
        arguments = _args_as_dict(params.get("arguments"))
        result = tool.handler(arguments)
        return _jsonrpc_result(
            message_id,
            {
                "content": [{"type": "text", "text": _tool_result_text(result)}],
                "isError": not result.ok,
            },
        )
    return _jsonrpc_error(message_id, -32601, f"Method not found: {method}")


def _read_stdin_messages():
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            error = _jsonrpc_error(None, -32700, "Parse error")
            sys.stdout.write(json.dumps(error, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            continue
        if isinstance(payload, dict):
            yield payload


def main() -> int:
    for message in _read_stdin_messages():
        response = handle_request(message)
        if response is None:
            continue
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
