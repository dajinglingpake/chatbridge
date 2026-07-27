from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from urllib.request import ProxyHandler, Request, build_opener

import psutil


CODEX_DESKTOP_PAGE_URL = "app://-/index.html"
DEBUGGER_RESPONSE_LIMIT = 1024 * 1024
REMOTE_DEBUGGING_PORT_RE = re.compile(r"^--remote-debugging-port(?:=(\d+))?$")


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
    const resumeResponse = await request('thread/resume', {{threadId}});
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
    payload = _run_desktop_action(
        expression,
        timeout_seconds=timeout_seconds,
        failure_message="无法向 Codex 历史会话发送消息",
    )
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
