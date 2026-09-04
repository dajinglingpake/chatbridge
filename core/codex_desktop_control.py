from __future__ import annotations

import json
import os
import queue
import re
import struct
import threading
import time
import uuid
from dataclasses import dataclass
from typing import BinaryIO
from urllib.request import ProxyHandler, Request, build_opener

import psutil


CODEX_DESKTOP_PAGE_URL = "app://-/index.html"
DEBUGGER_RESPONSE_LIMIT = 1024 * 1024
APP_TOOLS_FRAME_LIMIT = 8 * 1024 * 1024
APP_TOOLS_PIPE_ENV_VAR = "CODEX_APP_TOOLS_PIPE_PATH"
REMOTE_DEBUGGING_PORT_RE = re.compile(r"^--remote-debugging-port(?:=(\d+))?$")
APP_TOOLS_PIPE_RE = re.compile(r"^\\\\\.\\pipe\\codex-[A-Za-z0-9._-]+$")
ACTIVE_TURN_STATUSES = {"inprogress", "in_progress", "running"}


class CodexDesktopControlError(RuntimeError):
    pass


class CodexDesktopUnavailableError(CodexDesktopControlError):
    pass


@dataclass(frozen=True)
class CodexDesktopMessageResult:
    mode: str
    turn_id: str
    client_user_message_id: str = ""
    reconciled: bool = False


@dataclass(frozen=True)
class CodexDesktopGoalResult:
    action: str
    goal: dict[str, object]
    interrupted_turn_id: str = ""


def _remote_debugging_port(cmdline: object) -> int | None:
    if not isinstance(cmdline, (list, tuple)):
        return None
    args = [str(item or "").strip() for item in cmdline]
    for index, arg in enumerate(args):
        match = REMOTE_DEBUGGING_PORT_RE.match(arg)
        if match is None:
            continue
        raw_port = match.group(1) or (args[index + 1] if index + 1 < len(args) else "")
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            return None
        return port if 0 < port <= 65535 else None
    return None


def _codex_desktop_debugging_ports() -> list[int]:
    ports: list[int] = []
    for process in psutil.process_iter(("name", "exe", "cmdline")):
        try:
            info = process.info
            name = str(info.get("name") or "").strip().lower()
            cmdline = info.get("cmdline")
            location = " ".join(
                [str(info.get("exe") or ""), *(str(item or "") for item in cmdline or [])]
            ).replace("\\", "/").lower()
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        if name not in {"chatgpt.exe", "codex.exe", "chatgpt", "codex"}:
            continue
        if "openai.codex_" not in location and "/openai/codex/" not in location:
            continue
        port = _remote_debugging_port(cmdline)
        if port is not None and port not in ports:
            ports.append(port)
    return ports


def _normalized_app_tools_pipe_path(value: object) -> str:
    path = str(value or "").strip().strip('"')
    return path if APP_TOOLS_PIPE_RE.fullmatch(path) else ""


def _codex_desktop_app_tools_pipe_paths() -> list[str]:
    paths: list[str] = []
    for process in psutil.process_iter(("name", "exe", "cmdline")):
        try:
            info = process.info
            cmdline = info.get("cmdline") or []
            location = " ".join(
                [str(info.get("exe") or ""), *(str(item or "") for item in cmdline)]
            ).replace("\\", "/").lower()
            if (
                "openai.codex_" not in location
                and "/openai/codex/" not in location
                and "codex-app-tools" not in location
            ):
                continue
            environment = process.environ()
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
            continue
        path = _normalized_app_tools_pipe_path(environment.get(APP_TOOLS_PIPE_ENV_VAR))
        if path and path not in paths:
            paths.append(path)

    inherited_path = _normalized_app_tools_pipe_path(os.environ.get(APP_TOOLS_PIPE_ENV_VAR))
    if inherited_path and inherited_path not in paths:
        paths.append(inherited_path)
    return paths


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise CodexDesktopUnavailableError("Codex app-tools 管道已关闭")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _native_pipe_request_sync(
    pipe_path: str,
    method: str,
    params: dict[str, object],
) -> object:
    request_id = uuid.uuid4().int & 0x7FFFFFFF
    request_payload = json.dumps(
        {
            "id": request_id,
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(request_payload) > APP_TOOLS_FRAME_LIMIT:
        raise CodexDesktopControlError("Codex app-tools 请求过大")

    try:
        with open(pipe_path, "r+b", buffering=0) as stream:
            stream.write(struct.pack("<I", len(request_payload)) + request_payload)
            frame_size = struct.unpack("<I", _read_exact(stream, 4))[0]
            if frame_size > APP_TOOLS_FRAME_LIMIT:
                raise CodexDesktopControlError("Codex app-tools 响应过大")
            response = json.loads(_read_exact(stream, frame_size).decode("utf-8"))
    except CodexDesktopControlError:
        raise
    except (OSError, ValueError, json.JSONDecodeError, struct.error) as exc:
        raise CodexDesktopUnavailableError(f"无法连接 Codex app-tools 管道：{exc}") from exc

    if not isinstance(response, dict) or response.get("id") != request_id:
        raise CodexDesktopControlError("Codex app-tools 返回了无效响应")
    error = response.get("error")
    if isinstance(error, dict):
        raise CodexDesktopControlError(str(error.get("message") or error))
    if "result" not in response:
        raise CodexDesktopControlError("Codex app-tools 响应缺少 result")
    return response["result"]


def _native_pipe_request(
    pipe_path: str,
    method: str,
    params: dict[str, object],
    *,
    timeout_seconds: float,
) -> object:
    results: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            results.put((True, _native_pipe_request_sync(pipe_path, method, params)))
        except Exception as exc:
            results.put((False, exc))

    threading.Thread(target=worker, name="codex-app-tools", daemon=True).start()
    try:
        succeeded, result = results.get(timeout=max(0.2, timeout_seconds))
    except queue.Empty as exc:
        raise CodexDesktopControlError("Codex app-tools 请求超时") from exc
    if succeeded:
        return result
    if isinstance(result, Exception):
        raise result
    raise CodexDesktopControlError("Codex app-tools 请求失败")


def _run_codex_app_tools_request(
    method: str,
    params: dict[str, object],
    *,
    timeout_seconds: float,
) -> object:
    pipe_paths = _codex_desktop_app_tools_pipe_paths()
    if not pipe_paths:
        raise CodexDesktopUnavailableError("未发现 Codex Desktop app-tools 管道")

    unavailable_errors: list[str] = []
    for pipe_path in pipe_paths:
        try:
            return _native_pipe_request(
                pipe_path,
                method,
                params,
                timeout_seconds=timeout_seconds,
            )
        except CodexDesktopUnavailableError as exc:
            unavailable_errors.append(str(exc))
    detail = " | ".join(dict.fromkeys(unavailable_errors)) or "未知错误"
    raise CodexDesktopUnavailableError(f"Codex Desktop app-tools 不可用：{detail}")


def _app_tool_content_text(payload: dict[str, object]) -> str:
    content_items = payload.get("contentItems")
    if not isinstance(content_items, list):
        return ""
    return "\n".join(
        str(item.get("text") or "").strip()
        for item in content_items
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    )


def _call_codex_app_tool(
    tool: str,
    arguments: dict[str, object],
    *,
    caller_thread_id: str,
    timeout_seconds: float,
    call_id: str = "",
) -> dict[str, object]:
    cleaned_call_id = call_id or f"chatbridge:{uuid.uuid4()}"
    payload = _run_codex_app_tools_request(
        "tools/call",
        {
            "arguments": arguments,
            "callId": cleaned_call_id,
            "namespace": "codex_app",
            "threadId": caller_thread_id,
            "tool": tool,
            "turnId": f"chatbridge:{uuid.uuid4()}",
        },
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(payload, dict):
        raise CodexDesktopControlError("Codex app-tools 返回了无效工具响应")
    if not bool(payload.get("success")):
        raise CodexDesktopControlError(_app_tool_content_text(payload) or f"Codex 工具调用失败：{tool}")
    return payload


def _app_tool_json_content(payload: dict[str, object]) -> dict[str, object]:
    text = _app_tool_content_text(payload)
    if not text:
        return {}
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _read_codex_app_thread_state(thread_id: str, *, timeout_seconds: float) -> dict[str, object]:
    payload = _call_codex_app_tool(
        "read_thread",
        {
            "threadId": thread_id,
            "turnLimit": 2,
            "includeOutputs": False,
        },
        caller_thread_id=thread_id,
        timeout_seconds=timeout_seconds,
    )
    state = _app_tool_json_content(payload)
    if not state:
        raise CodexDesktopControlError("Codex read_thread 未返回会话状态")
    return state


def _thread_turns(state: dict[str, object]) -> list[dict[str, object]]:
    turns = state.get("turns")
    return [item for item in turns if isinstance(item, dict)] if isinstance(turns, list) else []


def _active_turn_id(state: dict[str, object]) -> str:
    for turn in _thread_turns(state):
        status = str(turn.get("status") or "").replace(" ", "").lower()
        if status in ACTIVE_TURN_STATUSES:
            return str(turn.get("id") or "").strip()
    return ""


def _latest_turn_id(state: dict[str, object]) -> str:
    turns = _thread_turns(state)
    return str(turns[0].get("id") or "").strip() if turns else ""


def _debugger_pages(port: int, *, timeout_seconds: float) -> list[dict[str, object]]:
    opener = build_opener(ProxyHandler({}))
    request = Request(
        f"http://127.0.0.1:{port}/json",
        headers={"User-Agent": "ChatBridge Codex desktop control"},
    )
    with opener.open(request, timeout=max(0.2, timeout_seconds)) as response:
        raw = response.read(DEBUGGER_RESPONSE_LIMIT + 1)
    if len(raw) > DEBUGGER_RESPONSE_LIMIT:
        raise CodexDesktopControlError("Codex 桌面调试页面响应过大")
    payload = json.loads(raw.decode("utf-8"))
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def _codex_desktop_websocket_urls(*, timeout_seconds: float) -> list[str]:
    urls: list[str] = []
    for port in _codex_desktop_debugging_ports():
        try:
            pages = _debugger_pages(port, timeout_seconds=min(timeout_seconds, 2.0))
        except (OSError, ValueError, json.JSONDecodeError, CodexDesktopControlError):
            continue
        for page in pages:
            if str(page.get("type") or "") != "page" or str(page.get("url") or "") != CODEX_DESKTOP_PAGE_URL:
                continue
            websocket_url = str(page.get("webSocketDebuggerUrl") or "").strip()
            if websocket_url and websocket_url not in urls:
                urls.append(websocket_url)
    return urls


def _desktop_bridge_expression(operation: str, *, timeout_seconds: float) -> str:
    timeout_ms = max(2000, int(timeout_seconds * 1000))
    return f"""
(async () => {{
    const bridge = window.electronBridge;
    if (!bridge || typeof bridge.sendMessageFromView !== 'function') {{
        return JSON.stringify({{ok: false, error: 'Codex 桌面桥接不可用'}});
    }}
    const request = (method, params, requestTimeoutMs = {timeout_ms}) => new Promise((resolve) => {{
        const id = `chatbridge:${{method}}:${{crypto.randomUUID()}}`;
        let settled = false;
        const finish = (message) => {{
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            window.removeEventListener('message', onMessage);
            resolve(message);
        }};
        const onMessage = (event) => {{
            const data = event.data;
            if (data?.type === 'mcp-response' && data?.hostId === 'local' && data?.message?.id === id) {{
                finish(data.message);
            }}
        }};
        const timer = setTimeout(
            () => finish({{error: {{message: `Codex 桌面请求超时：${{method}}`}}}}),
            requestTimeoutMs,
        );
        window.addEventListener('message', onMessage);
        bridge.sendMessageFromView({{
            type: 'mcp-request',
            hostId: 'local',
            request: {{id, method, params}},
        }}).catch((error) => finish({{error: {{message: String(error)}}}}));
    }});

{operation}
}})()
""".strip()


def _interrupt_expression(thread_id: str, *, timeout_seconds: float) -> str:
    encoded_thread_id = json.dumps(thread_id, ensure_ascii=True)
    operation = f"""
    const threadId = {encoded_thread_id};
    const turnsResponse = await request('thread/turns/list', {{
        threadId,
        limit: 1,
        sortDirection: 'desc',
        itemsView: 'notLoaded',
    }});
    if (turnsResponse?.error) {{
        return JSON.stringify({{ok: false, error: turnsResponse.error.message || String(turnsResponse.error)}});
    }}
    const turns = Array.isArray(turnsResponse?.result?.data) ? turnsResponse.result.data : [];
    const activeTurn = turns.find((turn) => ['inProgress', 'in_progress', 'running'].includes(String(turn?.status || '')));
    const turnId = String(activeTurn?.id || '');
    if (!turnId) {{
        return JSON.stringify({{ok: false, error: '该 Codex 历史会话当前没有可停止的轮次'}});
    }}

    const interruptResponse = await request('turn/interrupt', {{threadId, turnId}});
    if (interruptResponse?.error) {{
        return JSON.stringify({{ok: false, error: interruptResponse.error.message || String(interruptResponse.error)}});
    }}
    return JSON.stringify({{ok: true, threadId, turnId}});
""".strip()
    return _desktop_bridge_expression(operation, timeout_seconds=timeout_seconds)


def _message_expression(
    thread_id: str,
    text: str,
    images: list[str],
    *,
    client_user_message_id: str,
    timeout_seconds: float,
    model: str = "",
    reasoning_effort: str = "",
) -> str:
    input_items: list[dict[str, str]] = [{"type": "text", "text": text}]
    input_items.extend({"type": "localImage", "path": image} for image in images)
    encoded_thread_id = json.dumps(thread_id, ensure_ascii=True)
    encoded_input = json.dumps(input_items, ensure_ascii=True)
    encoded_message_id = json.dumps(client_user_message_id, ensure_ascii=True)
    encoded_model = json.dumps(str(model or "").strip(), ensure_ascii=True)
    encoded_reasoning_effort = json.dumps(str(reasoning_effort or "").strip(), ensure_ascii=True)
    operation = f"""
    const threadId = {encoded_thread_id};
    const input = {encoded_input};
    const clientUserMessageId = {encoded_message_id};
    const model = {encoded_model};
    const reasoningEffort = {encoded_reasoning_effort};
    const resumeResponse = await request('thread/resume', {{
        threadId,
        excludeTurns: true,
    }});
    if (resumeResponse?.error) {{
        return JSON.stringify({{ok: false, error: resumeResponse.error.message || String(resumeResponse.error)}});
    }}
    const startTurn = async () => {{
        const params = {{threadId, input, clientUserMessageId}};
        if (model) params.model = model;
        if (reasoningEffort) params.effort = reasoningEffort;
        return request('turn/start', params);
    }};
    const reconcileAcceptedMessage = async () => {{
        const recentResponse = await request('thread/turns/list', {{
            threadId,
            limit: 2,
            sortDirection: 'desc',
            itemsView: 'summary',
        }}, Math.min({max(2000, int(timeout_seconds * 1000))}, 4000));
        if (recentResponse?.error) return null;
        const recentTurns = Array.isArray(recentResponse?.result?.data) ? recentResponse.result.data : [];
        for (const turn of recentTurns) {{
            const items = Array.isArray(turn?.items) ? turn.items : [];
            const accepted = items.some(
                (item) => item?.type === 'userMessage' && String(item?.clientId || '') === clientUserMessageId,
            );
            const turnId = String(turn?.id || '');
            if (accepted && turnId) return turnId;
        }}
        return null;
    }};
    const turnsResponse = await request('thread/turns/list', {{
        threadId,
        limit: 1,
        sortDirection: 'desc',
        itemsView: 'notLoaded',
    }});
    if (turnsResponse?.error) {{
        return JSON.stringify({{ok: false, error: turnsResponse.error.message || String(turnsResponse.error)}});
    }}
    const turns = Array.isArray(turnsResponse?.result?.data) ? turnsResponse.result.data : [];
    const activeTurn = turns.find((turn) => ['inProgress', 'in_progress', 'running'].includes(String(turn?.status || '')));
    const activeTurnId = String(activeTurn?.id || '');
    let mode = activeTurnId ? 'steer' : 'start';
    let response = activeTurnId
        ? await request('turn/steer', {{
            threadId,
            expectedTurnId: activeTurnId,
            input,
            clientUserMessageId,
        }})
        : await startTurn();
    if (response?.error && activeTurnId) {{
        const refreshedTurnsResponse = await request('thread/turns/list', {{
            threadId,
            limit: 1,
            sortDirection: 'desc',
            itemsView: 'notLoaded',
        }});
        const refreshedTurns = Array.isArray(refreshedTurnsResponse?.result?.data)
            ? refreshedTurnsResponse.result.data
            : [];
        const refreshedActiveTurn = refreshedTurns.find(
            (turn) => ['inProgress', 'in_progress', 'running'].includes(String(turn?.status || '')),
        );
        const refreshedActiveTurnId = String(refreshedActiveTurn?.id || '');
        if (!refreshedTurnsResponse?.error && refreshedActiveTurnId !== activeTurnId) {{
            mode = refreshedActiveTurnId ? 'steer' : 'start';
            response = refreshedActiveTurnId
                ? await request('turn/steer', {{
                    threadId,
                    expectedTurnId: refreshedActiveTurnId,
                    input,
                    clientUserMessageId,
                }})
                : await startTurn();
        }}
    }}
    if (response?.error) {{
        const reconciledTurnId = await reconcileAcceptedMessage();
        if (reconciledTurnId) {{
            return JSON.stringify({{
                ok: true,
                threadId,
                turnId: reconciledTurnId,
                mode,
                reconciled: true,
                originalError: response.error.message || String(response.error),
            }});
        }}
        return JSON.stringify({{ok: false, error: response.error.message || String(response.error), mode}});
    }}
    let turnId = String(mode === 'steer' ? response?.result?.turnId || '' : response?.result?.turn?.id || '');
    if (!turnId) {{
        turnId = await reconcileAcceptedMessage() || '';
        if (!turnId) {{
            return JSON.stringify({{ok: false, error: 'Codex 桌面桥接未返回 turn_id', mode}});
        }}
        return JSON.stringify({{ok: true, threadId, turnId, mode, reconciled: true}});
    }}
    return JSON.stringify({{ok: true, threadId, turnId, mode, reconciled: false}});
""".strip()
    return _desktop_bridge_expression(operation, timeout_seconds=timeout_seconds)


def _goal_expression(
    thread_id: str,
    action: str,
    objective: str,
    *,
    timeout_seconds: float,
) -> str:
    encoded_thread_id = json.dumps(thread_id, ensure_ascii=True)
    encoded_action = json.dumps(action, ensure_ascii=True)
    encoded_objective = json.dumps(objective, ensure_ascii=True)
    operation = f"""
    const threadId = {encoded_thread_id};
    const action = {encoded_action};
    const objective = {encoded_objective};
    const resumeResponse = await request('thread/resume', {{threadId, excludeTurns: true}});
    if (resumeResponse?.error) {{
        return JSON.stringify({{ok: false, error: resumeResponse.error.message || String(resumeResponse.error)}});
    }}

    if (action === 'delete') {{
        const clearResponse = await request('thread/goal/clear', {{threadId}});
        if (clearResponse?.error) {{
            return JSON.stringify({{ok: false, error: clearResponse.error.message || String(clearResponse.error)}});
        }}
        return JSON.stringify({{ok: true, threadId, action, goal: {{threadId, status: 'cleared'}}}});
    }}

    const currentResponse = await request('thread/goal/get', {{threadId}});
    if (currentResponse?.error) {{
        return JSON.stringify({{ok: false, error: currentResponse.error.message || String(currentResponse.error)}});
    }}
    const currentGoal = currentResponse?.result?.goal || null;
    const status = action === 'pause' ? 'paused' : 'active';
    const params = {{threadId, status}};
    if (objective) params.objective = objective;
    if (!currentGoal && !params.objective) {{
        return JSON.stringify({{ok: false, error: '该 Codex 历史会话当前没有可控制的目标'}});
    }}

    const setResponse = await request('thread/goal/set', params);
    if (setResponse?.error) {{
        return JSON.stringify({{ok: false, error: setResponse.error.message || String(setResponse.error)}});
    }}
    let goal = setResponse?.result?.goal || {{...currentGoal, ...params}};

    const turnsResponse = await request('thread/turns/list', {{
        threadId,
        limit: 1,
        sortDirection: 'desc',
        itemsView: 'notLoaded',
    }});
    const turns = Array.isArray(turnsResponse?.result?.data) ? turnsResponse.result.data : [];
    const activeTurn = turns.find((turn) => ['inProgress', 'in_progress', 'running'].includes(String(turn?.status || '')));
    const activeTurnId = String(activeTurn?.id || '');
    let interruptedTurnId = '';

    if (action === 'pause' && activeTurnId) {{
        const interruptResponse = await request('turn/interrupt', {{threadId, turnId: activeTurnId}});
        if (interruptResponse?.error) {{
            return JSON.stringify({{ok: false, error: interruptResponse.error.message || String(interruptResponse.error)}});
        }}
        interruptedTurnId = activeTurnId;
    }} else if (status === 'active' && !activeTurnId) {{
        await new Promise((resolve) => setTimeout(resolve, 250));
        const continueResponse = await request('thread/goal/set', {{threadId, status: 'active'}});
        if (continueResponse?.error) {{
            return JSON.stringify({{ok: false, error: continueResponse.error.message || String(continueResponse.error)}});
        }}
        goal = continueResponse?.result?.goal || goal;
    }}

    return JSON.stringify({{ok: true, threadId, action, goal, interruptedTurnId}});
""".strip()
    return _desktop_bridge_expression(operation, timeout_seconds=timeout_seconds)


def _goal_get_expression(thread_id: str, *, timeout_seconds: float) -> str:
    encoded_thread_id = json.dumps(thread_id, ensure_ascii=True)
    operation = f"""
    const threadId = {encoded_thread_id};
    const response = await request('thread/goal/get', {{threadId}});
    if (response?.error) {{
        return JSON.stringify({{ok: false, error: response.error.message || String(response.error)}});
    }}
    return JSON.stringify({{ok: true, threadId, goal: response?.result?.goal || null}});
""".strip()
    return _desktop_bridge_expression(operation, timeout_seconds=timeout_seconds)


def _evaluate_cdp(websocket_url: str, expression: str, *, timeout_seconds: float) -> object:
    try:
        import websocket
    except ImportError as exc:
        raise CodexDesktopControlError("缺少 websocket-client，无法连接 Codex 桌面版") from exc

    connection = websocket.create_connection(
        websocket_url,
        timeout=max(1.0, timeout_seconds + 2.0),
        suppress_origin=True,
    )
    request_id = uuid.uuid4().int & 0x7FFFFFFF
    deadline = time.monotonic() + max(1.0, timeout_seconds + 2.0)
    try:
        connection.send(
            json.dumps(
                {
                    "id": request_id,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": expression,
                        "awaitPromise": True,
                        "returnByValue": True,
                    },
                }
            )
        )
        while time.monotonic() < deadline:
            connection.settimeout(max(0.1, deadline - time.monotonic()))
            message = json.loads(connection.recv())
            if not isinstance(message, dict) or message.get("id") != request_id:
                continue
            if isinstance(message.get("error"), dict):
                raise CodexDesktopControlError(str(message["error"].get("message") or message["error"]))
            result = message.get("result") if isinstance(message.get("result"), dict) else {}
            if result.get("exceptionDetails"):
                raise CodexDesktopControlError("Codex 桌面桥接执行失败")
            remote_result = result.get("result") if isinstance(result.get("result"), dict) else {}
            return remote_result.get("value")
    finally:
        connection.close()
    raise CodexDesktopControlError("等待 Codex 桌面桥接响应超时")


def _run_desktop_action(
    expression: str,
    *,
    timeout_seconds: float,
    failure_message: str,
) -> dict[str, object]:
    websocket_urls = _codex_desktop_websocket_urls(timeout_seconds=timeout_seconds)
    if not websocket_urls:
        raise CodexDesktopUnavailableError("未发现可安全控制的 Codex 桌面窗口")

    errors: list[str] = []
    for websocket_url in websocket_urls:
        try:
            raw_result = _evaluate_cdp(websocket_url, expression, timeout_seconds=timeout_seconds)
            payload = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
            if not isinstance(payload, dict):
                raise CodexDesktopControlError("Codex 桌面桥接返回了无效响应")
            if not bool(payload.get("ok")):
                raise CodexDesktopControlError(str(payload.get("error") or failure_message))
            return payload
        except Exception as exc:
            errors.append(str(exc))
    unique_errors = list(dict.fromkeys(error for error in errors if error))
    detail = " | ".join(unique_errors[:3]) if unique_errors else "未知错误"
    raise CodexDesktopControlError(f"{failure_message}：{detail}")


def interrupt_codex_desktop_thread(thread_id: str, *, timeout_seconds: float = 15.0) -> str:
    cleaned_thread_id = str(thread_id or "").strip()
    if not cleaned_thread_id:
        raise CodexDesktopControlError("thread_id 不能为空")
    expression = _interrupt_expression(cleaned_thread_id, timeout_seconds=timeout_seconds)
    payload = _run_desktop_action(
        expression,
        timeout_seconds=timeout_seconds,
        failure_message="无法停止 Codex 历史会话",
    )
    turn_id = str(payload.get("turnId") or "").strip()
    if not turn_id:
        raise CodexDesktopControlError("Codex 桌面桥接未返回 turn_id")
    return turn_id


def _send_codex_app_tools_thread_message(
    thread_id: str,
    text: str,
    *,
    images: list[str],
    model: str,
    reasoning_effort: str,
    timeout_seconds: float,
) -> CodexDesktopMessageResult:
    if images:
        raise CodexDesktopControlError("新版 Codex Desktop app-tools 暂不支持跨任务图片附件")

    probe_timeout = min(max(1.0, timeout_seconds), 4.0)
    before_state = _read_codex_app_thread_state(thread_id, timeout_seconds=probe_timeout)
    before_active_turn_id = _active_turn_id(before_state)
    before_latest_turn_id = _latest_turn_id(before_state)
    client_user_message_id = f"chatbridge:{uuid.uuid4()}"
    arguments: dict[str, object] = {
        "threadId": thread_id,
        "prompt": text,
    }
    if model:
        arguments["model"] = model
    if reasoning_effort:
        arguments["thinking"] = reasoning_effort
    send_payload = _call_codex_app_tool(
        "send_message_to_thread",
        arguments,
        caller_thread_id=thread_id,
        timeout_seconds=timeout_seconds,
        call_id=client_user_message_id,
    )

    send_result = _app_tool_json_content(send_payload)
    returned_turn_id = str(send_result.get("turnId") or send_result.get("turn_id") or "").strip()
    after_state = _read_codex_app_thread_state(thread_id, timeout_seconds=probe_timeout)
    after_active_turn_id = _active_turn_id(after_state)
    after_latest_turn_id = _latest_turn_id(after_state)

    if before_active_turn_id and after_active_turn_id in {"", before_active_turn_id}:
        mode = "steer"
        turn_id = returned_turn_id or before_active_turn_id
    elif before_active_turn_id and after_active_turn_id:
        mode = "start"
        turn_id = returned_turn_id or after_active_turn_id
    else:
        mode = "start"
        changed_turn_id = after_latest_turn_id if after_latest_turn_id != before_latest_turn_id else ""
        turn_id = returned_turn_id or after_active_turn_id or changed_turn_id
    if not turn_id:
        raise CodexDesktopControlError("Codex app-tools 已接收消息，但未能确认目标轮次")
    return CodexDesktopMessageResult(
        mode=mode,
        turn_id=turn_id,
        client_user_message_id=client_user_message_id,
        reconciled=True,
    )


def send_codex_desktop_thread_message(
    thread_id: str,
    text: str,
    *,
    images: list[str] | None = None,
    model: str = "",
    reasoning_effort: str = "",
    timeout_seconds: float = 15.0,
) -> CodexDesktopMessageResult:
    cleaned_thread_id = str(thread_id or "").strip()
    if not cleaned_thread_id:
        raise CodexDesktopControlError("thread_id 不能为空")
    cleaned_text = str(text or "").strip()
    if not cleaned_text:
        raise CodexDesktopControlError("消息内容不能为空")
    cleaned_images = list(
        dict.fromkeys(str(item or "").strip() for item in (images or []) if str(item or "").strip())
    )
    client_user_message_id = f"chatbridge:{uuid.uuid4()}"
    expression = _message_expression(
        cleaned_thread_id,
        cleaned_text,
        cleaned_images,
        client_user_message_id=client_user_message_id,
        timeout_seconds=timeout_seconds,
        model=str(model or "").strip(),
        reasoning_effort=str(reasoning_effort or "").strip(),
    )
    cdp_error: CodexDesktopControlError | None = None
    try:
        payload = _run_desktop_action(
            expression,
            timeout_seconds=timeout_seconds,
            failure_message="无法向 Codex 历史会话发送消息",
        )
    except CodexDesktopUnavailableError:
        return _send_codex_app_tools_thread_message(
            cleaned_thread_id,
            cleaned_text,
            images=cleaned_images,
            model=str(model or "").strip(),
            reasoning_effort=str(reasoning_effort or "").strip(),
            timeout_seconds=timeout_seconds,
        )
    except CodexDesktopControlError as exc:
        cdp_error = exc
        try:
            return _send_codex_app_tools_thread_message(
                cleaned_thread_id,
                cleaned_text,
                images=cleaned_images,
                model=str(model or "").strip(),
                reasoning_effort=str(reasoning_effort or "").strip(),
                timeout_seconds=timeout_seconds,
            )
        except CodexDesktopUnavailableError:
            raise cdp_error
    mode = str(payload.get("mode") or "").strip()
    turn_id = str(payload.get("turnId") or "").strip()
    if mode not in {"steer", "start"}:
        raise CodexDesktopControlError("Codex 桌面桥接未返回有效发送模式")
    if not turn_id:
        raise CodexDesktopControlError("Codex 桌面桥接未返回 turn_id")
    return CodexDesktopMessageResult(
        mode=mode,
        turn_id=turn_id,
        client_user_message_id=client_user_message_id,
        reconciled=bool(payload.get("reconciled")),
    )


def control_codex_desktop_thread_goal(
    thread_id: str,
    action: str,
    *,
    objective: str = "",
    timeout_seconds: float = 15.0,
) -> CodexDesktopGoalResult:
    cleaned_thread_id = str(thread_id or "").strip()
    if not cleaned_thread_id:
        raise CodexDesktopControlError("thread_id 不能为空")
    cleaned_action = str(action or "").strip().lower()
    if cleaned_action not in {"pause", "resume", "edit", "delete"}:
        raise CodexDesktopControlError(f"不支持的目标操作：{cleaned_action or '-'}")
    cleaned_objective = str(objective or "").strip()
    if cleaned_action == "edit" and not cleaned_objective:
        raise CodexDesktopControlError("目标内容不能为空")
    expression = _goal_expression(
        cleaned_thread_id,
        cleaned_action,
        cleaned_objective,
        timeout_seconds=timeout_seconds,
    )
    payload = _run_desktop_action(
        expression,
        timeout_seconds=timeout_seconds,
        failure_message="无法控制 Codex 历史会话目标",
    )
    goal = payload.get("goal") if isinstance(payload.get("goal"), dict) else {}
    return CodexDesktopGoalResult(
        action=cleaned_action,
        goal=dict(goal),
        interrupted_turn_id=str(payload.get("interruptedTurnId") or "").strip(),
    )


def get_codex_desktop_thread_goal(
    thread_id: str,
    *,
    timeout_seconds: float = 5.0,
) -> dict[str, object] | None:
    cleaned_thread_id = str(thread_id or "").strip()
    if not cleaned_thread_id:
        raise CodexDesktopControlError("thread_id 不能为空")
    expression = _goal_get_expression(cleaned_thread_id, timeout_seconds=timeout_seconds)
    payload = _run_desktop_action(
        expression,
        timeout_seconds=timeout_seconds,
        failure_message="无法读取 Codex 历史会话目标",
    )
    goal = payload.get("goal")
    return dict(goal) if isinstance(goal, dict) else None
