from __future__ import annotations

import json
import os
import queue
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_backends.base import BackendContext, McpServerConfig
from agent_backends.codex_backend import CodexBackend, _CodexAppServerClient
from agent_hub import AgentConfig, HubConfig, MultiCodexHub
from core.state_models import HubTask


class RecordingBackend:
    key = "codex"

    def __init__(self) -> None:
        self.last_agent = None
        self.last_context = None
        self.last_session_name = ""
        self.last_prompt = ""

    def invoke(self, agent, prompt: str, session_name: str, context) -> dict[str, str]:
        self.last_agent = agent
        self.last_context = context
        self.last_session_name = session_name
        self.last_prompt = prompt
        return {"output": "ok", "session_id": "mgr-session-1"}


class RecordingCodexThreadBackend(RecordingBackend):
    def __init__(self) -> None:
        super().__init__()
        self.last_list_context = None
        self.last_read_context = None
        self.last_send_context = None
        self.last_send_payload = None
        self.last_archived = None

    def list_app_server_threads(self, context, *, limit: int, cursor: str, search_term: str, cwd: str, archived: bool | None = False) -> dict[str, object]:
        self.last_list_context = context
        self.last_archived = archived
        return {
            "threads": [
                {
                    "id": "thread-1",
                    "title": "真实会话",
                    "cwd": "I:/AI/chatbridge",
                    "preview": "继续真实任务",
                }
            ],
            "next_cursor": "",
            "backwards_cursor": "",
        }

    def read_app_server_thread(self, context, thread_id: str) -> dict[str, object]:
        self.last_read_context = context
        return {
            "id": thread_id,
            "title": "真实会话",
            "messages": [{"role": "reasoning", "text": "思考摘要"}],
        }

    def send_app_server_thread_message(self, context, thread_id: str, prompt: str, **kwargs) -> dict[str, object]:
        self.last_send_context = context
        self.last_send_payload = {"thread_id": thread_id, "prompt": prompt, **kwargs}
        return {
            "thread_id": thread_id,
            "turn_id": "turn-2",
            "mode": "start",
            "client_user_message_id": "chatbridge:message-2",
            "reconciled": False,
        }

class McpServerInjectionTests(unittest.TestCase):
    def test_codex_app_server_routes_responses_without_consuming_turn_events(self) -> None:
        client = _CodexAppServerClient("codex", creationflags=0, start_new_session=False, slim_exec=True)
        response_queue: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
        client._response_queues[7] = response_queue
        turn_queue = client._subscribe_turn("thread-1", "turn-1")

        client._dispatch_message({"id": 7, "result": {"ok": True}})
        client._dispatch_message(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "completed"},
                },
            }
        )

        self.assertEqual({"ok": True}, response_queue.get_nowait()["result"])
        self.assertEqual("turn/completed", turn_queue.get_nowait()["method"])
        self.assertTrue(client._messages.empty())

    def test_codex_app_server_bounds_unclaimed_notifications(self) -> None:
        client = _CodexAppServerClient("codex", creationflags=0, start_new_session=False, slim_exec=True)

        for index in range(700):
            client._dispatch_message(
                {
                    "method": "thread/status/changed",
                    "params": {"threadId": f"thread-{index}", "status": "idle"},
                }
            )

        self.assertEqual(512, client._messages.qsize())

    def test_codex_app_server_history_message_starts_turn_and_preserves_options(self) -> None:
        requests: list[tuple[str, dict[str, object], float]] = []

        class FakeClient:
            def request(self, method: str, params: dict[str, object], *, timeout: float) -> dict[str, object]:
                requests.append((method, params, timeout))
                if method == "thread/read":
                    return {"thread": {"id": "thread-1", "path": "C:/tmp/rollout-thread-1.jsonl"}}
                if method == "thread/resume":
                    return {"thread": {"id": "thread-1"}}
                if method == "thread/turns/list":
                    return {"data": [{"id": "turn-old", "status": "completed"}]}
                if method == "turn/start":
                    return {"turn": {"id": "turn-new", "status": "inProgress"}}
                raise AssertionError(method)

        backend = CodexBackend()
        context = BackendContext(
            codex_command="codex.cmd",
            claude_command="claude",
            opencode_command="opencode",
            session_dir=Path("sessions"),
            creationflags=0,
        )
        with (
            patch.object(backend, "_get_app_server", return_value=FakeClient()),
            patch("agent_backends.codex_backend.threading.Thread") as thread,
        ):
            result = backend.send_app_server_thread_message(
                context,
                " thread-1 ",
                " 继续检查 ",
                images=[" C:/tmp/shot.png ", "C:/tmp/shot.png"],
                model=" gpt-5.6-sol ",
                reasoning_effort=" ultra ",
                timeout_seconds=9,
            )

        self.assertEqual("start", result["mode"])
        self.assertEqual("turn-new", result["turn_id"])
        self.assertTrue(str(result["client_user_message_id"]).startswith("chatbridge:"))
        turn_start = next(params for method, params, _ in requests if method == "turn/start")
        self.assertEqual(
            [
                {"type": "text", "text": "继续检查"},
                {"type": "localImage", "path": "C:/tmp/shot.png"},
            ],
            turn_start["input"],
        )
        self.assertEqual("gpt-5.6-sol", turn_start["model"])
        self.assertEqual("ultra", turn_start["effort"])
        self.assertEqual(
            {
                "threadId": "thread-1",
                "path": "C:/tmp/rollout-thread-1.jsonl",
                "excludeTurns": True,
            },
            next(params for method, params, _ in requests if method == "thread/resume"),
        )
        methods = [method for method, _, _ in requests]
        self.assertLess(methods.index("thread/read"), methods.index("thread/resume"))
        self.assertLess(methods.index("thread/resume"), methods.index("thread/turns/list"))
        thread.return_value.start.assert_called_once_with()

    def test_codex_app_server_streams_reasoning_separately_from_final_answer(self) -> None:
        live_output: list[str] = []
        reasoning: list[str] = []
        activities: list[dict[str, object]] = []
        client = _CodexAppServerClient("codex", creationflags=0, start_new_session=False, slim_exec=True)
        client._messages.put(
            {
                "method": "item/reasoning/summaryTextDelta",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "reasoning-1",
                    "summaryIndex": 0,
                    "delta": "先检查项目结构",
                },
            }
        )
        client._messages.put(
            {
                "method": "item/started",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "startedAtMs": 1783161500000,
                    "item": {
                        "id": "command-1",
                        "type": "commandExecution",
                        "command": "pytest -q",
                        "cwd": "I:/AI/chatbridge",
                        "status": "inProgress",
                        "commandActions": [],
                    },
                },
            }
        )
        client._messages.put(
            {
                "method": "item/commandExecution/outputDelta",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "command-1",
                    "delta": "623 passed\n",
                },
            }
        )
        client._messages.put(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "completedAtMs": 1783161501200,
                    "item": {
                        "id": "command-1",
                        "type": "commandExecution",
                        "command": "pytest -q",
                        "cwd": "I:/AI/chatbridge",
                        "status": "completed",
                        "commandActions": [],
                        "aggregatedOutput": "623 passed\n",
                        "exitCode": 0,
                        "durationMs": 1200,
                    },
                },
            }
        )
        client._messages.put(
            {
                "method": "item/reasoning/summaryTextDelta",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "reasoning-2",
                    "summaryIndex": 0,
                    "delta": "完成测试结果核对",
                },
            }
        )
        client._messages.put(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "answer-1",
                    "delta": "最终回答片段",
                },
            }
        )
        client._messages.put(
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "tokenUsage": {
                        "total": {"totalTokens": 25, "inputTokens": 20, "cachedInputTokens": 0, "outputTokens": 5, "reasoningOutputTokens": 0},
                        "last": {"totalTokens": 25, "inputTokens": 20, "cachedInputTokens": 0, "outputTokens": 5, "reasoningOutputTokens": 0},
                        "modelContextWindow": 100,
                    },
                },
            }
        )
        client._messages.put(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "completedAtMs": 1,
                    "item": {"id": "answer-1", "type": "agentMessage", "text": "最终回答"},
                },
            }
        )
        client._messages.put(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "completed", "items": []},
                },
            }
        )

        output, context_left_percent = client.wait_for_turn(
            "thread-1",
            "turn-1",
            timeout=1,
            on_progress=None,
            on_live_output=live_output.append,
            on_reasoning=reasoning.append,
            on_activity=activities.append,
        )

        self.assertEqual("最终回答", output)
        self.assertEqual(75, context_left_percent)
        self.assertEqual(["先检查项目结构", "先检查项目结构\n\n完成测试结果核对"], reasoning)
        self.assertEqual(["最终回答片段", "最终回答"], live_output)
        self.assertEqual(
            ["codex_reasoning", "codex_command", "codex_command", "codex_command", "codex_reasoning"],
            [activity["event"] for activity in activities],
        )
        self.assertEqual(["reasoning-1", "reasoning-2"], [activity["id"] for activity in activities if activity["event"] == "codex_reasoning"])
        command_activity = [activity for activity in activities if activity["event"] == "codex_command"][-1]
        self.assertEqual("success", command_activity["type"])
        self.assertEqual("pytest -q", command_activity["detail"])
        self.assertEqual("623 passed\n", command_activity["metadata"]["output"])
        self.assertEqual(0, command_activity["metadata"]["exit_code"])

    def test_codex_app_server_normalizes_threads_and_reasoning_history(self) -> None:
        backend = CodexBackend()
        thread = {
            "id": "thread-1",
            "sessionId": "thread-1",
            "preview": "继续真实任务",
            "cwd": "I:/AI/chatbridge",
            "source": "vscode",
            "modelProvider": "openai",
            "createdAt": 1783076542,
            "updatedAt": 1783161563,
            "status": {"type": "notLoaded"},
            "gitInfo": {"branch": "main", "sha": "abc123"},
            "turns": [
                {
                    "id": "turn-1",
                    "createdAt": 1783161500,
                    "items": [
                        {"type": "userMessage", "id": "item-user", "content": [{"type": "text", "text": "用户问题"}]},
                        {"type": "reasoning", "id": "item-reasoning", "summary": ["先检查状态"]},
                        {
                            "type": "tool_call",
                            "id": "item-tool",
                            "callId": "call-1",
                            "name": "shell",
                            "status": "completed",
                            "detail": {"type": "shell", "command": "pytest", "log": "ok"},
                            "completedAtMs": 1783161564,
                        },
                        {
                            "type": "todo",
                            "id": "item-todo",
                            "items": [{"text": "修复滚动", "completed": True}],
                            "completedAtMs": 1783161565,
                        },
                        {
                            "type": "compaction",
                            "id": "item-compaction",
                            "status": "completed",
                            "trigger": "auto",
                            "preTokens": 12345,
                            "completedAtMs": 1783161566,
                        },
                        {"type": "agentMessage", "id": "item-answer", "text": "最终回答", "phase": "final_answer"},
                    ],
                }
            ],
        }

        normalized = backend._normalize_app_server_thread(thread)
        messages = backend._normalize_app_server_thread_messages(thread)

        self.assertEqual("thread-1", normalized["id"])
        self.assertEqual("继续真实任务", normalized["title"])
        self.assertEqual("I:/AI/chatbridge", normalized["cwd"])
        self.assertEqual("main", normalized["branch"])
        self.assertEqual(["user", "reasoning", "activity", "activity", "activity", "assistant"], [message["role"] for message in messages])
        self.assertEqual(["用户问题", "先检查状态", "shell: pytest", "[x] 修复滚动", "completed", "最终回答"], [message["text"] for message in messages])
        self.assertEqual([1, 1, 1, 1, 1, 1], [message["turn_order"] for message in messages])
        self.assertEqual([1, 2, 3, 4, 5, 6], [message["item_order"] for message in messages])
        turn_at = backend._format_app_server_timestamp(1783161500)
        self.assertEqual(turn_at, messages[0]["at"])
        self.assertEqual(turn_at, messages[1]["at"])
        self.assertEqual(turn_at, messages[-1]["at"])
        self.assertNotEqual(turn_at, messages[2]["at"])
        activities = [message["activity"] for message in messages if message["role"] == "activity"]
        self.assertEqual(["codex_tool_call", "codex_todo", "codex_compaction"], [activity["event"] for activity in activities])
        self.assertEqual(messages[2]["at"], activities[0]["at"])
        self.assertEqual("shell", activities[0]["metadata"]["name"])
        self.assertEqual("auto", activities[2]["metadata"]["trigger"])

    def test_codex_app_server_normalizes_mcp_tool_calls_for_history(self) -> None:
        message = CodexBackend._normalize_app_server_item(
            {
                "type": "mcpToolCall",
                "id": "exec-1",
                "server": "node_repl",
                "tool": "js",
                "status": "completed",
                "arguments": {"title": "运行测试", "code": "run_tests()"},
                "result": {"content": [{"type": "text", "text": "633 tests passed"}]},
                "durationMs": 1200,
            },
            turn_id="turn-1",
            turn_order=1,
            item_order=3,
            fallback_at="2026-07-17T02:00:00",
        )

        self.assertIsNotNone(message)
        assert message is not None
        self.assertEqual("activity", message["role"])
        self.assertEqual("codex_tool_call", message["activity"]["event"])
        self.assertEqual("success", message["activity"]["type"])
        self.assertEqual("node_repl.js - 运行测试\nrun_tests()", message["activity"]["detail"])
        self.assertEqual("node_repl.js - 运行测试\nrun_tests()", message["activity"]["metadata"]["command"])
        self.assertEqual("node_repl", message["activity"]["metadata"]["server"])
        self.assertEqual("js", message["activity"]["metadata"]["tool"])
        self.assertEqual("633 tests passed", message["activity"]["metadata"]["output"])
        self.assertEqual("1200", message["activity"]["metadata"]["durationMs"])

    def test_codex_app_server_normalizes_command_execution_history(self) -> None:
        activity = CodexBackend._app_server_activity_payload(
            {
                "id": "command-1",
                "type": "commandExecution",
                "command": "pytest -q",
                "cwd": "I:/AI/chatbridge",
                "status": "completed",
                "aggregatedOutput": "623 passed\n",
                "exitCode": 0,
                "durationMs": 1200,
            },
            item_type="commandExecution",
            item_id="command-1",
        )

        self.assertEqual("codex_command", activity["event"])
        self.assertEqual("pytest -q", activity["detail"])
        self.assertEqual("pytest -q", activity["metadata"]["command"])
        self.assertEqual("623 passed\n", activity["metadata"]["output"])
        self.assertEqual("0", activity["metadata"]["exitCode"])

    def test_codex_exec_json_extracts_command_activity(self) -> None:
        activity = CodexBackend._extract_exec_command_activity(
            {
                "type": "item.completed",
                "item": {
                    "id": "item-1",
                    "type": "command_execution",
                    "command": "git status --short",
                    "status": "completed",
                    "aggregated_output": " M ui/app.py\n",
                    "exit_code": 0,
                },
            }
        )

        self.assertEqual("codex_command", activity["event"])
        self.assertEqual("git status --short", activity["metadata"]["command"])
        self.assertEqual(" M ui/app.py\n", activity["metadata"]["output"])

    def test_hub_exposes_codex_threads_through_app_server_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workdir = temp_path / "workspace"
            session_file = temp_path / "sessions" / "main.txt"
            workdir.mkdir(parents=True, exist_ok=True)
            session_file.parent.mkdir(parents=True, exist_ok=True)
            config = HubConfig(
                codex_command="codex.cmd",
                claude_command="claude",
                opencode_command="opencode",
                agents=[AgentConfig("main", "Main", str(workdir), str(session_file), backend="codex")],
            )
            backend = RecordingCodexThreadBackend()
            with (
                patch("agent_hub.STATE_PATH", temp_path / "state" / "agent_hub_state.json"),
                patch("agent_hub.discover_external_agent_processes", return_value=[]),
                patch("agent_hub.resolve_codex_app_server_command", return_value="codex.cmd"),
            ):
                hub = MultiCodexHub(config)
                hub.backend_registry["codex"] = backend

                listed = hub.list_codex_threads(limit=10, archived=True)
                read = hub.read_codex_thread("thread-1")
                sent = hub.send_codex_thread_message(
                    "thread-1",
                    "继续",
                    images=["C:/tmp/shot.png"],
                    model="gpt-5.6-sol",
                    reasoning_effort="ultra",
                    timeout_seconds=9,
                )

        self.assertEqual("thread-1", listed["threads"][0]["id"])
        self.assertEqual("thread-1", read["id"])
        self.assertEqual("app-server", backend.last_list_context.codex_transport)
        self.assertIs(True, backend.last_archived)
        self.assertEqual("codex.cmd", backend.last_read_context.codex_command)
        self.assertEqual("turn-2", sent["turn_id"])
        self.assertEqual("codex.cmd", backend.last_send_context.codex_command)
        self.assertEqual("继续", backend.last_send_payload["prompt"])
        self.assertEqual(["C:/tmp/shot.png"], backend.last_send_payload["images"])
        self.assertEqual("gpt-5.6-sol", backend.last_send_payload["model"])
        self.assertEqual("ultra", backend.last_send_payload["reasoning_effort"])

    def test_wechat_task_injects_mcp_server_into_backend_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workdir = temp_path / "workspace"
            session_file = temp_path / "sessions" / "main.txt"
            workdir.mkdir(parents=True, exist_ok=True)
            session_file.parent.mkdir(parents=True, exist_ok=True)
            config = HubConfig(
                codex_command="codex",
                claude_command="claude",
                opencode_command="opencode",
                agents=[AgentConfig("main", "Main", str(workdir), str(session_file), backend="codex")],
            )
            backend = RecordingBackend()
            with (
                patch("agent_hub.STATE_PATH", temp_path / "state" / "agent_hub_state.json"),
                patch("agent_hub.discover_external_agent_processes", return_value=[]),
            ):
                hub = MultiCodexHub(config)
                hub.backend_registry["codex"] = backend
                task = HubTask(
                    id="task-wechat-001",
                    agent_id="main",
                    agent_name="Main",
                    backend="codex",
                    source="wechat",
                    sender_id="sender-test",
                    prompt="列出所有会话",
                    status="queued",
                    created_at="2026-04-20T20:00:00",
                    session_name="default",
                )

                result = hub._invoke_backend(config.agents[0], task)

            self.assertEqual("ok", result["output"])
            self.assertEqual("mgr-session-1", result["session_id"])
            self.assertEqual("列出所有会话", backend.last_prompt)
            self.assertEqual("default", backend.last_session_name)
            self.assertEqual("列出所有会话", task.prompt)
            self.assertIsNotNone(backend.last_context.mcp_server)
            self.assertEqual("operations", backend.last_context.mcp_server.name)
            self.assertNotIn("--trusted-internal-manager", backend.last_context.mcp_server.args)
            self.assertEqual("Main", backend.last_agent.name)
            self.assertEqual("main", backend.last_agent.id)
            self.assertTrue(backend.last_context.codex_slim_exec)
            self.assertEqual("exec", backend.last_context.codex_transport)

    def test_hub_passes_codex_slim_exec_setting_to_web_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workdir = temp_path / "workspace"
            session_file = temp_path / "sessions" / "main.txt"
            workdir.mkdir(parents=True, exist_ok=True)
            session_file.parent.mkdir(parents=True, exist_ok=True)
            config = HubConfig(
                codex_command="codex",
                claude_command="claude",
                opencode_command="opencode",
                codex_slim_exec=False,
                codex_transport="app-server",
                agents=[AgentConfig("main", "Main", str(workdir), str(session_file), backend="codex")],
            )
            backend = RecordingBackend()
            with (
                patch("agent_hub.STATE_PATH", temp_path / "state" / "agent_hub_state.json"),
                patch("agent_hub.discover_external_agent_processes", return_value=[]),
            ):
                hub = MultiCodexHub(config)
                hub.backend_registry["codex"] = backend
                task = HubTask(
                    id="task-local-slim-001",
                    agent_id="main",
                    agent_name="Main",
                    backend="codex",
                    source="stream-web",
                    sender_id="",
                    prompt="hello",
                    status="queued",
                    created_at="2026-04-20T20:00:00",
                    session_name="default",
                )

                hub._invoke_backend(config.agents[0], task)

            self.assertFalse(backend.last_context.codex_slim_exec)
            self.assertEqual("app-server", backend.last_context.codex_transport)

    def test_hub_forces_non_web_codex_tasks_to_exec_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workdir = temp_path / "workspace"
            session_file = temp_path / "sessions" / "main.txt"
            workdir.mkdir(parents=True, exist_ok=True)
            session_file.parent.mkdir(parents=True, exist_ok=True)
            config = HubConfig(
                codex_command="codex",
                claude_command="claude",
                opencode_command="opencode",
                codex_transport="app-server",
                agents=[AgentConfig("main", "Main", str(workdir), str(session_file), backend="codex")],
            )
            backend = RecordingBackend()
            with (
                patch("agent_hub.STATE_PATH", temp_path / "state" / "agent_hub_state.json"),
                patch("agent_hub.discover_external_agent_processes", return_value=[]),
            ):
                hub = MultiCodexHub(config)
                hub.backend_registry["codex"] = backend
                for source in ("wechat", "qq", "qq-web", "cli"):
                    task = HubTask(
                        id=f"task-{source}",
                        agent_id="main",
                        agent_name="Main",
                        backend="codex",
                        source=source,
                        sender_id="sender-test",
                        prompt="hello",
                        status="queued",
                        created_at="2026-04-20T20:00:00",
                        session_name="default",
                    )
                    hub._invoke_backend(config.agents[0], task)
                    self.assertEqual("exec", backend.last_context.codex_transport, source)

    def test_hub_allows_desktop_codex_tasks_to_use_app_server_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workdir = temp_path / "workspace"
            session_file = temp_path / "sessions" / "main.txt"
            workdir.mkdir(parents=True, exist_ok=True)
            session_file.parent.mkdir(parents=True, exist_ok=True)
            config = HubConfig(
                codex_command="codex",
                claude_command="claude",
                opencode_command="opencode",
                codex_transport="app-server",
                agents=[AgentConfig("main", "Main", str(workdir), str(session_file), backend="codex")],
            )
            backend = RecordingBackend()
            with (
                patch("agent_hub.STATE_PATH", temp_path / "state" / "agent_hub_state.json"),
                patch("agent_hub.discover_external_agent_processes", return_value=[]),
            ):
                hub = MultiCodexHub(config)
                hub.backend_registry["codex"] = backend
                task = HubTask(
                    id="task-desktop",
                    agent_id="main",
                    agent_name="Main",
                    backend="codex",
                    source="desktop",
                    sender_id="sender-test",
                    prompt="hello",
                    status="queued",
                    created_at="2026-04-20T20:00:00",
                    session_name="default",
                )

                hub._invoke_backend(config.agents[0], task)

            self.assertEqual("app-server", backend.last_context.codex_transport)

    def test_hub_passes_selected_codex_thread_id_to_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workdir = temp_path / "workspace"
            session_file = temp_path / "sessions" / "main.txt"
            workdir.mkdir(parents=True, exist_ok=True)
            session_file.parent.mkdir(parents=True, exist_ok=True)
            config = HubConfig(
                codex_command="codex",
                claude_command="claude",
                opencode_command="opencode",
                codex_transport="app-server",
                agents=[AgentConfig("main", "Main", str(workdir), str(session_file), backend="codex")],
            )
            backend = RecordingBackend()
            with (
                patch("agent_hub.STATE_PATH", temp_path / "state" / "agent_hub_state.json"),
                patch("agent_hub.discover_external_agent_processes", return_value=[]),
            ):
                hub = MultiCodexHub(config)
                hub.backend_registry["codex"] = backend
                task = HubTask(
                    id="task-resume-thread-001",
                    agent_id="main",
                    agent_name="Main",
                    backend="codex",
                    source="stream-web",
                    sender_id="",
                    prompt="继续",
                    status="queued",
                    created_at="2026-04-20T20:00:00",
                    session_name="codex-thread-1",
                    session_id="thread-1",
                )

                hub._invoke_backend(config.agents[0], task)

        self.assertEqual("thread-1", backend.last_context.codex_thread_id)

    def test_wechat_task_passes_bridge_state_overrides_to_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workdir = temp_path / "workspace"
            session_file = temp_path / "sessions" / "main.txt"
            workdir.mkdir(parents=True, exist_ok=True)
            session_file.parent.mkdir(parents=True, exist_ok=True)
            config = HubConfig(
                codex_command="codex",
                claude_command="claude",
                opencode_command="opencode",
                agents=[AgentConfig("main", "Main", str(workdir), str(session_file), backend="codex")],
            )
            backend = RecordingBackend()
            conversation_path = temp_path / ".runtime" / "state" / "weixin_conversations.json"
            event_log_path = temp_path / ".runtime" / "logs" / "weixin_bridge_events.jsonl"
            with (
                patch("agent_hub.STATE_PATH", temp_path / "state" / "agent_hub_state.json"),
                patch("agent_hub.discover_external_agent_processes", return_value=[]),
            ):
                hub = MultiCodexHub(config)
                hub.backend_registry["codex"] = backend
                task = HubTask(
                    id="task-wechat-002",
                    agent_id="main",
                    agent_name="Main",
                    backend="codex",
                    source="wechat",
                    sender_id="sender-test",
                    prompt="列出所有会话",
                    status="queued",
                    created_at="2026-04-20T20:00:00",
                    session_name="default",
                    bridge_conversations_path=str(conversation_path),
                    bridge_event_log_path=str(event_log_path),
                )

                hub._invoke_backend(config.agents[0], task)

            self.assertIsNotNone(backend.last_context.mcp_server)
            self.assertIn("--bridge-conversations-path", backend.last_context.mcp_server.args)
            self.assertIn(str(conversation_path), backend.last_context.mcp_server.args)
            self.assertIn("--bridge-event-log-path", backend.last_context.mcp_server.args)
            self.assertIn(str(event_log_path), backend.last_context.mcp_server.args)

    def test_non_wechat_task_does_not_mount_mcp_server(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workdir = temp_path / "workspace"
            session_file = temp_path / "sessions" / "main.txt"
            workdir.mkdir(parents=True, exist_ok=True)
            session_file.parent.mkdir(parents=True, exist_ok=True)
            config = HubConfig(
                codex_command="codex",
                claude_command="claude",
                opencode_command="opencode",
                agents=[AgentConfig("main", "Main", str(workdir), str(session_file), backend="codex")],
            )
            backend = RecordingBackend()
            with (
                patch("agent_hub.STATE_PATH", temp_path / "state" / "agent_hub_state.json"),
                patch("agent_hub.discover_external_agent_processes", return_value=[]),
            ):
                hub = MultiCodexHub(config)
                hub.backend_registry["codex"] = backend
                task = HubTask(
                    id="task-local-001",
                    agent_id="main",
                    agent_name="Main",
                    backend="codex",
                    source="cli",
                    sender_id="",
                    prompt="hello",
                    status="queued",
                    created_at="2026-04-20T20:00:00",
                    session_name="default",
                )

                hub._invoke_backend(config.agents[0], task)

            self.assertIsNone(backend.last_context.mcp_server)

    def test_qq_group_task_injects_current_group_history_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workdir = temp_path / "workspace"
            session_file = temp_path / "sessions" / "qq-group.txt"
            workdir.mkdir(parents=True, exist_ok=True)
            session_file.parent.mkdir(parents=True, exist_ok=True)
            config = HubConfig(
                codex_command="codex",
                claude_command="claude",
                opencode_command="opencode",
                agents=[AgentConfig("qq-group", "QQ Group", str(workdir), str(session_file), backend="codex")],
            )
            backend = RecordingBackend()
            with (
                patch("agent_hub.STATE_PATH", temp_path / "state" / "agent_hub_state.json"),
                patch("agent_hub.discover_external_agent_processes", return_value=[]),
            ):
                hub = MultiCodexHub(config)
                hub.backend_registry["codex"] = backend
                task = HubTask(
                    id="task-qq-group-001",
                    agent_id="qq-group",
                    agent_name="QQ Group",
                    backend="codex",
                    source="qq",
                    sender_id="qq:group:811708184",
                    prompt="看看群里刚才说了什么",
                    status="queued",
                    created_at="2026-04-20T20:00:00",
                    session_name="default",
                )

                hub._invoke_backend(config.agents[0], task)

            self.assertIsNotNone(backend.last_context.mcp_server)
            self.assertEqual("qq_history", backend.last_context.mcp_server.name)
            self.assertIn("--qq-history-scope", backend.last_context.mcp_server.args)
            self.assertIn("group", backend.last_context.mcp_server.args)
            self.assertIn("--qq-group-id", backend.last_context.mcp_server.args)
            self.assertIn("811708184", backend.last_context.mcp_server.args)
            self.assertEqual("exec", backend.last_context.codex_transport)

    def test_qq_private_non_admin_does_not_mount_history_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workdir = temp_path / "workspace"
            session_file = temp_path / "sessions" / "qq.txt"
            workdir.mkdir(parents=True, exist_ok=True)
            session_file.parent.mkdir(parents=True, exist_ok=True)
            config = HubConfig(
                codex_command="codex",
                claude_command="claude",
                opencode_command="opencode",
                agents=[AgentConfig("qq", "QQ", str(workdir), str(session_file), backend="codex")],
            )
            backend = RecordingBackend()
            with (
                patch("agent_hub.STATE_PATH", temp_path / "state" / "agent_hub_state.json"),
                patch("agent_hub.discover_external_agent_processes", return_value=[]),
                patch("agent_hub.BridgeConfig.load", return_value=SimpleNamespace(qq_allowed_private_user_ids=["10001"])),
            ):
                hub = MultiCodexHub(config)
                hub.backend_registry["codex"] = backend
                task = HubTask(
                    id="task-qq-private-001",
                    agent_id="qq",
                    agent_name="QQ",
                    backend="codex",
                    source="qq",
                    sender_id="qq:private:20002",
                    prompt="hello",
                    status="queued",
                    created_at="2026-04-20T20:00:00",
                    session_name="default",
                )

                hub._invoke_backend(config.agents[0], task)

            self.assertIsNone(backend.last_context.mcp_server)
            self.assertEqual("exec", backend.last_context.codex_transport)

    def test_qq_private_admin_injects_full_operations_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workdir = temp_path / "workspace"
            session_file = temp_path / "sessions" / "qq.txt"
            workdir.mkdir(parents=True, exist_ok=True)
            session_file.parent.mkdir(parents=True, exist_ok=True)
            config = HubConfig(
                codex_command="codex",
                claude_command="claude",
                opencode_command="opencode",
                agents=[AgentConfig("qq", "QQ", str(workdir), str(session_file), backend="codex")],
            )
            backend = RecordingBackend()
            with (
                patch("agent_hub.STATE_PATH", temp_path / "state" / "agent_hub_state.json"),
                patch("agent_hub.discover_external_agent_processes", return_value=[]),
                patch("agent_hub.BridgeConfig.load", return_value=SimpleNamespace(qq_allowed_private_user_ids=["10001"])),
            ):
                hub = MultiCodexHub(config)
                hub.backend_registry["codex"] = backend
                task = HubTask(
                    id="task-qq-private-002",
                    agent_id="qq",
                    agent_name="QQ",
                    backend="codex",
                    source="qq",
                    sender_id="qq:private:10001",
                    prompt="查一下群历史",
                    status="queued",
                    created_at="2026-04-20T20:00:00",
                    session_name="default",
                )

                hub._invoke_backend(config.agents[0], task)

            self.assertIsNotNone(backend.last_context.mcp_server)
            self.assertEqual("operations", backend.last_context.mcp_server.name)
            self.assertNotIn("--qq-history-scope", backend.last_context.mcp_server.args)
            self.assertIn("--current-sender-id", backend.last_context.mcp_server.args)
            self.assertIn("qq:private:10001", backend.last_context.mcp_server.args)
            self.assertIn("--qq-admin-user-id", backend.last_context.mcp_server.args)
            self.assertIn("10001", backend.last_context.mcp_server.args)
            self.assertEqual("exec", backend.last_context.codex_transport)


class McpServerCodexBackendTests(unittest.TestCase):
    def test_codex_backend_injects_mcp_server_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workdir = temp_path / "workspace"
            workdir.mkdir(parents=True, exist_ok=True)
            session_dir = temp_path / "sessions"
            session_dir.mkdir(parents=True, exist_ok=True)
            output_path = temp_path / "multi-codex-output-fixed.txt"

            agent = SimpleNamespace(
                id="main",
                name="Main",
                workdir=str(workdir),
                session_file=str(session_dir / "main.txt"),
                backend="codex",
                model="gpt-5.4",
                prompt_prefix="system",
            )
            context_left_values: list[int] = []
            context = BackendContext(
                codex_command="codex",
                claude_command="claude",
                opencode_command="opencode",
                session_dir=session_dir,
                creationflags=0,
                on_context_left_percent=context_left_values.append,
                mcp_server=McpServerConfig(
                    name="operations",
                    command="python3",
                    args=["/tmp/operations_server.py"],
                ),
            )
            backend = CodexBackend()

            class FakeProcess:
                def __init__(self, argv: list[str]) -> None:
                    self.argv = argv
                    self.pid = 4321
                    self.stdout = iter(
                        [
                            '{"type":"thread.started","thread_id":"thread-1"}\n',
                            (
                                '{"type":"event_msg","payload":{"type":"token_count","info":'
                                '{"last_token_usage":{"total_tokens":100000},"model_context_window":250000}}}\n'
                            ),
                        ]
                    )
                    self.stderr = iter([])

                def wait(self) -> int:
                    return 0

            def fake_popen(argv: list[str], **kwargs):
                output_path.write_text("ok", encoding="utf-8")
                self.assertIn("--disable", argv)
                self.assertIn("plugins", argv)
                self.assertIn("--ignore-rules", argv)
                self.assertIn('mcp_servers.operations.command="python3"', argv)
                self.assertIn('mcp_servers.operations.args=["/tmp/operations_server.py"]', argv)
                self.assertIn('mcp_servers.operations.default_tools_approval_mode="approve"', argv)
                return FakeProcess(argv)

            with (
                patch("agent_backends.codex_backend.tempfile.gettempdir", return_value=str(temp_path)),
                patch("agent_backends.codex_backend.uuid.uuid4", return_value=SimpleNamespace(hex="fixed")),
                patch("agent_backends.codex_backend.subprocess.Popen", side_effect=fake_popen),
            ):
                result = backend.invoke(agent, "hello", "", context)

            self.assertEqual("ok", result["output"])
            self.assertEqual("thread-1", result["session_id"])
            self.assertEqual("60", result["context_left_percent"])
            self.assertEqual([60], context_left_values)

    def test_codex_backend_app_server_approves_injected_mcp_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workdir = temp_path / "workspace"
            workdir.mkdir(parents=True, exist_ok=True)
            session_dir = temp_path / "sessions"
            session_dir.mkdir(parents=True, exist_ok=True)
            agent = SimpleNamespace(
                id="main",
                name="Main",
                workdir=str(workdir),
                session_file=str(session_dir / "main.txt"),
                backend="codex",
                model="gpt-5.4",
                prompt_prefix="system",
            )
            context = BackendContext(
                codex_command="codex",
                claude_command="claude",
                opencode_command="opencode",
                session_dir=session_dir,
                creationflags=0,
                mcp_server=McpServerConfig(
                    name="operations",
                    command="python3",
                    args=["/tmp/operations_server.py"],
                ),
            )
            backend = CodexBackend()

            params = backend._app_server_thread_params(agent, workdir, context)

            mcp_server = params["config"]["mcp_servers"]["operations"]
            self.assertEqual("python3", mcp_server["command"])
            self.assertEqual(["/tmp/operations_server.py"], mcp_server["args"])
            self.assertEqual("approve", mcp_server["default_tools_approval_mode"])

    def test_codex_backend_applies_reasoning_effort_and_default_permission_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workdir = temp_path / "workspace"
            workdir.mkdir(parents=True, exist_ok=True)
            session_dir = temp_path / "sessions"
            session_dir.mkdir(parents=True, exist_ok=True)
            output_path = temp_path / "multi-codex-output-fixed.txt"

            agent = SimpleNamespace(
                id="main",
                name="Main",
                workdir=str(workdir),
                session_file=str(session_dir / "main.txt"),
                backend="codex",
                model="gpt-5.4",
                prompt_prefix="system",
            )
            context = BackendContext(
                codex_command="codex",
                claude_command="claude",
                opencode_command="opencode",
                session_dir=session_dir,
                creationflags=0,
                reasoning_effort="high",
                permission_mode="default",
            )
            backend = CodexBackend()

            class FakeProcess:
                def __init__(self) -> None:
                    self.pid = 4321
                    self.stdout = iter(['{"type":"thread.started","thread_id":"thread-2"}\n'])
                    self.stderr = iter([])

                def wait(self) -> int:
                    return 0

            def fake_popen(argv: list[str], **kwargs):
                output_path.write_text("ok", encoding="utf-8")
                self.assertIn('-c', argv)
                self.assertIn('model_reasoning_effort="high"', argv)
                self.assertIn('approval_policy="never"', argv)
                self.assertIn('-s', argv)
                self.assertIn('workspace-write', argv)
                self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', argv)
                return FakeProcess()

            with (
                patch("agent_backends.codex_backend.tempfile.gettempdir", return_value=str(temp_path)),
                patch("agent_backends.codex_backend.uuid.uuid4", return_value=SimpleNamespace(hex="fixed")),
                patch("agent_backends.codex_backend.subprocess.Popen", side_effect=fake_popen),
            ):
                result = backend.invoke(agent, "hello", "", context)

            self.assertEqual("ok", result["output"])
            self.assertEqual("thread-2", result["session_id"])

    def test_codex_backend_applies_read_only_permission_and_search(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workdir = temp_path / "workspace"
            workdir.mkdir(parents=True, exist_ok=True)
            session_dir = temp_path / "sessions"
            session_dir.mkdir(parents=True, exist_ok=True)
            output_path = temp_path / "multi-codex-output-fixed.txt"

            agent = SimpleNamespace(
                id="qq-group",
                name="QQ Group",
                workdir=str(workdir),
                session_file=str(session_dir / "qq-group.txt"),
                backend="codex",
                model="gpt-5.4",
                prompt_prefix="",
            )
            context = BackendContext(
                codex_command="codex",
                claude_command="claude",
                opencode_command="opencode",
                session_dir=session_dir,
                creationflags=0,
                permission_mode="read-only",
                codex_search_enabled=True,
            )
            backend = CodexBackend()

            class FakeProcess:
                def __init__(self) -> None:
                    self.pid = 4321
                    self.stdout = iter(['{"type":"thread.started","thread_id":"thread-readonly"}\n'])
                    self.stderr = iter([])

                def wait(self) -> int:
                    return 0

            def fake_popen(argv: list[str], **kwargs):
                output_path.write_text("ok", encoding="utf-8")
                self.assertIn('-c', argv)
                self.assertIn('approval_policy="never"', argv)
                self.assertIn('-s', argv)
                self.assertIn('read-only', argv)
                self.assertIn('web_search="live"', argv)
                self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', argv)
                return FakeProcess()

            with (
                patch("agent_backends.codex_backend.tempfile.gettempdir", return_value=str(temp_path)),
                patch("agent_backends.codex_backend.uuid.uuid4", return_value=SimpleNamespace(hex="fixed")),
                patch("agent_backends.codex_backend.subprocess.Popen", side_effect=fake_popen),
            ):
                result = backend.invoke(agent, "hello", "", context)

            self.assertEqual("ok", result["output"])
            self.assertEqual("thread-readonly", result["session_id"])

    def test_codex_backend_applies_permission_profile_images_and_search(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workdir = temp_path / "workspace"
            workdir.mkdir(parents=True, exist_ok=True)
            image_path = workdir / ".chatbridge_attachments" / "qq-group-1" / "image.png"
            image_path.parent.mkdir(parents=True, exist_ok=True)
            image_path.write_bytes(b"png")
            session_dir = temp_path / "sessions"
            session_dir.mkdir(parents=True, exist_ok=True)
            output_path = temp_path / "multi-codex-output-fixed.txt"

            agent = SimpleNamespace(
                id="qq-group",
                name="QQ Group",
                workdir=str(workdir),
                session_file=str(session_dir / "qq-group.txt"),
                backend="codex",
                model="gpt-5.4",
                prompt_prefix="",
            )
            context = BackendContext(
                codex_command="codex",
                claude_command="claude",
                opencode_command="opencode",
                session_dir=session_dir,
                creationflags=0,
                permission_mode="read-only",
                permission_profile="qq_group",
                codex_search_enabled=True,
                images=[str(image_path.relative_to(workdir))],
            )
            backend = CodexBackend()

            class FakeProcess:
                def __init__(self) -> None:
                    self.pid = 4321
                    self.stdout = iter(['{"type":"thread.started","thread_id":"thread-profile"}\n'])
                    self.stderr = iter([])

                def wait(self) -> int:
                    return 0

            def fake_popen(argv: list[str], **kwargs):
                output_path.write_text("ok", encoding="utf-8")
                self.assertIn('approval_policy="never"', argv)
                self.assertIn('default_permissions="qq_group"', argv)
                self.assertTrue(any(str(item).startswith("permissions.qq_group.filesystem=") for item in argv))
                self.assertTrue(any(str(item).startswith("permissions.qq_group.network=") for item in argv))
                self.assertIn('web_search="live"', argv)
                self.assertIn("-i", argv)
                self.assertIn(str(image_path.relative_to(workdir)), argv)
                self.assertNotIn("-s", argv)
                self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
                return FakeProcess()

            with (
                patch("agent_backends.codex_backend.tempfile.gettempdir", return_value=str(temp_path)),
                patch("agent_backends.codex_backend.uuid.uuid4", return_value=SimpleNamespace(hex="fixed")),
                patch("agent_backends.codex_backend.subprocess.Popen", side_effect=fake_popen),
            ):
                result = backend.invoke(agent, "hello", "", context)

            self.assertEqual("ok", result["output"])
            self.assertEqual("thread-profile", result["session_id"])

    def test_codex_backend_resume_uses_config_sandbox_instead_of_sandbox_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workdir = temp_path / "workspace"
            workdir.mkdir(parents=True, exist_ok=True)
            session_dir = temp_path / "sessions"
            session_dir.mkdir(parents=True, exist_ok=True)
            session_file = session_dir / "qq-group.txt"
            session_file.write_text("existing-session", encoding="utf-8")
            output_path = temp_path / "multi-codex-output-fixed.txt"

            agent = SimpleNamespace(
                id="qq-group",
                name="QQ Group",
                workdir=str(workdir),
                session_file=str(session_file),
                backend="codex",
                model="gpt-5.4",
                prompt_prefix="",
            )
            context = BackendContext(
                codex_command="codex",
                claude_command="claude",
                opencode_command="opencode",
                session_dir=session_dir,
                creationflags=0,
                permission_mode="read-only",
            )
            backend = CodexBackend()

            class FakeProcess:
                def __init__(self) -> None:
                    self.pid = 4321
                    self.stdout = iter([])
                    self.stderr = iter([])

                def wait(self) -> int:
                    return 0

            def fake_popen(argv: list[str], **kwargs):
                output_path.write_text("ok", encoding="utf-8")
                self.assertEqual(["codex", "exec", "resume"], argv[:3])
                self.assertIn('approval_policy="never"', argv)
                self.assertIn('sandbox_mode="read-only"', argv)
                self.assertNotIn("-s", argv)
                self.assertNotIn("-C", argv)
                self.assertIn("existing-session", argv)
                self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
                return FakeProcess()

            with (
                patch("agent_backends.codex_backend.tempfile.gettempdir", return_value=str(temp_path)),
                patch("agent_backends.codex_backend.uuid.uuid4", return_value=SimpleNamespace(hex="fixed")),
                patch("agent_backends.codex_backend.subprocess.Popen", side_effect=fake_popen),
            ):
                result = backend.invoke(agent, "hello", "", context)

            self.assertEqual("ok", result["output"])
            self.assertEqual("existing-session", result["session_id"])

    def test_codex_backend_retries_transient_stream_disconnect_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workdir = temp_path / "workspace"
            workdir.mkdir(parents=True, exist_ok=True)
            session_dir = temp_path / "sessions"
            session_dir.mkdir(parents=True, exist_ok=True)
            output_path = temp_path / "multi-codex-output-fixed.txt"
            progress: list[str] = []

            agent = SimpleNamespace(
                id="main",
                name="Main",
                workdir=str(workdir),
                session_file=str(session_dir / "main.txt"),
                backend="codex",
                model="",
                prompt_prefix="",
            )
            context = BackendContext(
                codex_command="codex",
                claude_command="claude",
                opencode_command="opencode",
                session_dir=session_dir,
                creationflags=0,
                on_progress=progress.append,
            )
            backend = CodexBackend()

            class FakeProcess:
                def __init__(self, returncode: int) -> None:
                    self.pid = 4321
                    self.returncode = returncode
                    self.stdout = iter(['{"type":"thread.started","thread_id":"thread-retry"}\n'])
                    self.stderr = iter([])

                def wait(self, timeout: float | None = None) -> int:
                    return self.returncode

            calls = {"count": 0}

            def fake_popen(argv: list[str], **kwargs):
                calls["count"] += 1
                if calls["count"] == 1:
                    return FakeProcess(1)
                output_path.write_text("ok after retry", encoding="utf-8")
                return FakeProcess(0)

            with (
                patch("agent_backends.codex_backend.tempfile.gettempdir", return_value=str(temp_path)),
                patch("agent_backends.codex_backend.uuid.uuid4", return_value=SimpleNamespace(hex="fixed")),
                patch("agent_backends.codex_backend.subprocess.Popen", side_effect=fake_popen),
            ):
                with patch.object(CodexBackend, "_wait_for_exit", side_effect=[RuntimeError("stream disconnected before completion"), 0]):
                    result = backend.invoke(agent, "hello", "", context)

            self.assertEqual("ok after retry", result["output"])
            self.assertEqual(2, calls["count"])
            self.assertIn("自动重试", progress[0])

    def test_codex_backend_retries_two_transient_stream_disconnects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workdir = temp_path / "workspace"
            workdir.mkdir(parents=True, exist_ok=True)
            session_dir = temp_path / "sessions"
            session_dir.mkdir(parents=True, exist_ok=True)
            output_path = temp_path / "multi-codex-output-fixed.txt"
            progress: list[str] = []

            agent = SimpleNamespace(
                id="main",
                name="Main",
                workdir=str(workdir),
                session_file=str(session_dir / "main.txt"),
                backend="codex",
                model="",
                prompt_prefix="",
            )
            context = BackendContext(
                codex_command="codex",
                claude_command="claude",
                opencode_command="opencode",
                session_dir=session_dir,
                creationflags=0,
                on_progress=progress.append,
            )
            backend = CodexBackend()

            class FakeProcess:
                def __init__(self, returncode: int) -> None:
                    self.pid = 4321
                    self.returncode = returncode
                    self.stdout = iter(['{"type":"thread.started","thread_id":"thread-retry"}\n'])
                    self.stderr = iter([])

                def wait(self, timeout: float | None = None) -> int:
                    return self.returncode

            calls = {"count": 0}

            def fake_popen(argv: list[str], **kwargs):
                calls["count"] += 1
                if calls["count"] < 3:
                    return FakeProcess(1)
                output_path.write_text("ok after second retry", encoding="utf-8")
                return FakeProcess(0)

            with (
                patch("agent_backends.codex_backend.tempfile.gettempdir", return_value=str(temp_path)),
                patch("agent_backends.codex_backend.uuid.uuid4", return_value=SimpleNamespace(hex="fixed")),
                patch("agent_backends.codex_backend.subprocess.Popen", side_effect=fake_popen),
            ):
                with patch.object(
                    CodexBackend,
                    "_wait_for_exit",
                    side_effect=[
                        RuntimeError("stream disconnected before completion"),
                        RuntimeError("stream disconnected before completion"),
                        0,
                    ],
                ):
                    result = backend.invoke(agent, "hello", "", context)

            self.assertEqual("ok after second retry", result["output"])
            self.assertEqual(3, calls["count"])
            self.assertEqual(2, len([item for item in progress if "自动重试" in item]))

    def test_codex_backend_does_not_retry_transient_error_after_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workdir = temp_path / "workspace"
            workdir.mkdir(parents=True, exist_ok=True)
            session_dir = temp_path / "sessions"
            session_dir.mkdir(parents=True, exist_ok=True)

            agent = SimpleNamespace(
                id="main",
                name="Main",
                workdir=str(workdir),
                session_file=str(session_dir / "main.txt"),
                backend="codex",
                model="",
                prompt_prefix="",
            )
            context = BackendContext(
                codex_command="codex",
                claude_command="claude",
                opencode_command="opencode",
                session_dir=session_dir,
                creationflags=0,
                is_cancel_requested=lambda: True,
            )
            backend = CodexBackend()

            class FakeProcess:
                pid = 4321
                stdout = iter(['{"type":"thread.started","thread_id":"thread-cancel"}\n'])
                stderr = iter([])

            calls = {"count": 0}

            def fake_popen(argv: list[str], **kwargs):
                calls["count"] += 1
                return FakeProcess()

            with (
                patch("agent_backends.codex_backend.tempfile.gettempdir", return_value=str(temp_path)),
                patch("agent_backends.codex_backend.subprocess.Popen", side_effect=fake_popen),
                patch.object(CodexBackend, "_wait_for_exit", side_effect=RuntimeError("stream disconnected before completion")),
                patch("agent_backends.codex_backend.terminate_process_tree") as terminate,
            ):
                with self.assertRaisesRegex(RuntimeError, "Task canceled during execution"):
                    backend.invoke(agent, "hello", "", context)

            self.assertEqual(1, calls["count"])
            terminate.assert_called_once_with(4321)

    def test_codex_backend_kills_process_on_exit_timeout(self) -> None:
        backend = CodexBackend()

        class HangingProcess:
            pid = 9876

            def wait(self, timeout: float | None = None) -> int:
                raise subprocess.TimeoutExpired("codex", timeout or 0)

        with patch("agent_backends.codex_backend.terminate_process_tree") as terminate:
            with self.assertRaisesRegex(RuntimeError, "timeout waiting for child process"):
                backend._wait_for_exit(HangingProcess())  # type: ignore[arg-type]

        terminate.assert_called_once_with(9876)

    def test_codex_backend_finishes_when_task_complete_event_precedes_process_exit(self) -> None:
        self._assert_codex_backend_finishes_after_complete_event(
            '{"type":"event_msg","payload":{"type":"task_complete"}}\n'
        )

    def test_codex_backend_finishes_when_turn_completed_event_precedes_process_exit(self) -> None:
        self._assert_codex_backend_finishes_after_complete_event('{"type":"turn.completed"}\n')

    def _assert_codex_backend_finishes_after_complete_event(self, complete_event: str) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workdir = temp_path / "workspace"
            workdir.mkdir(parents=True, exist_ok=True)
            session_dir = temp_path / "sessions"
            session_dir.mkdir(parents=True, exist_ok=True)

            agent = SimpleNamespace(
                id="main",
                name="Main",
                workdir=str(workdir),
                session_file=str(session_dir / "main.txt"),
                backend="codex",
                model="",
                prompt_prefix="",
            )
            context = BackendContext(
                codex_command="codex",
                claude_command="claude",
                opencode_command="opencode",
                session_dir=session_dir,
                creationflags=0,
                hub_task_timeout_seconds=30,
            )
            backend = CodexBackend()

            class CompletedButHangingProcess:
                pid = 9877
                stdout = iter(
                    [
                        '{"type":"thread.started","thread_id":"thread-complete"}\n',
                        complete_event,
                    ]
                )
                stderr = iter([])

                def __init__(self) -> None:
                    self.wait_calls = 0

                def poll(self) -> int | None:
                    return None

                def wait(self, timeout: float | None = None) -> int:
                    self.wait_calls += 1
                    if self.wait_calls == 1:
                        raise subprocess.TimeoutExpired("codex", timeout or 0)
                    return 0

            proc = CompletedButHangingProcess()

            def fake_popen(argv: list[str], **kwargs):
                output_path = Path(argv[argv.index("-o") + 1])
                output_path.write_text("ok after task complete", encoding="utf-8")
                return proc

            with (
                patch("agent_backends.codex_backend.tempfile.gettempdir", return_value=str(temp_path)),
                patch("agent_backends.codex_backend.uuid.uuid4", return_value=SimpleNamespace(hex="fixed")),
                patch("agent_backends.codex_backend.subprocess.Popen", side_effect=fake_popen),
                patch("agent_backends.codex_backend.terminate_process_tree") as terminate,
            ):
                result = backend.invoke(agent, "hello", "", context)

        self.assertEqual("ok after task complete", result["output"])
        self.assertEqual("thread-complete", result["session_id"])
        terminate.assert_called_once_with(9877)

    def test_codex_backend_times_out_when_stdout_stalls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workdir = temp_path / "workspace"
            workdir.mkdir(parents=True, exist_ok=True)
            session_dir = temp_path / "sessions"
            session_dir.mkdir(parents=True, exist_ok=True)

            agent = SimpleNamespace(
                id="main",
                name="Main",
                workdir=str(workdir),
                session_file=str(session_dir / "main.txt"),
                backend="codex",
                model="",
                prompt_prefix="",
            )
            context = BackendContext(
                codex_command="codex",
                claude_command="claude",
                opencode_command="opencode",
                session_dir=session_dir,
                creationflags=0,
                hub_task_timeout_seconds=1,
            )
            backend = CodexBackend()

            class BlockingStdout:
                def __iter__(self):
                    return self

                def __next__(self):
                    time.sleep(30)
                    raise StopIteration

            class HangingProcess:
                pid = 9877
                stdout = BlockingStdout()
                stderr = iter([])

                def poll(self) -> int | None:
                    return None

            with (
                patch("agent_backends.codex_backend.subprocess.Popen", return_value=HangingProcess()),
                patch("agent_backends.codex_backend.terminate_process_tree") as terminate,
            ):
                with self.assertRaisesRegex(RuntimeError, "timed out after 1 seconds"):
                    backend._invoke_once(agent, "hello", "", context)

        terminate.assert_called_once_with(9877)

    def test_codex_backend_recovers_stdout_idle_timeout_by_resuming_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workdir = temp_path / "workspace"
            workdir.mkdir(parents=True, exist_ok=True)
            session_dir = temp_path / "sessions"
            session_dir.mkdir(parents=True, exist_ok=True)
            session_file = session_dir / "main.txt"
            progress: list[str] = []

            agent = SimpleNamespace(
                id="main",
                name="Main",
                workdir=str(workdir),
                session_file=str(session_file),
                backend="codex",
                model="",
                prompt_prefix="",
            )
            context = BackendContext(
                codex_command="codex",
                claude_command="claude",
                opencode_command="opencode",
                session_dir=session_dir,
                creationflags=0,
                hub_task_timeout_seconds=1,
                on_progress=progress.append,
            )
            backend = CodexBackend()

            class StallingStdout:
                def __iter__(self):
                    return self

                def __init__(self) -> None:
                    self.started = False

                def __next__(self) -> str:
                    if not self.started:
                        self.started = True
                        return '{"type":"thread.started","thread_id":"thread-timeout"}\n'
                    time.sleep(30)
                    raise StopIteration

            class StallingProcess:
                pid = 9879
                stdout = StallingStdout()
                stderr = iter([])

                def poll(self) -> int | None:
                    return None

            class SuccessProcess:
                pid = 9880
                stdout = iter(['{"type":"thread.started","thread_id":"thread-timeout"}\n'])
                stderr = iter([])

                def poll(self) -> int | None:
                    return 0

                def wait(self, timeout: float | None = None) -> int:
                    return 0

            calls: list[list[str]] = []

            def fake_popen(argv: list[str], **kwargs):
                calls.append(argv)
                if len(calls) == 1:
                    return StallingProcess()
                output_path = Path(argv[argv.index("-o") + 1])
                output_path.write_text("ok after recovery", encoding="utf-8")
                return SuccessProcess()

            with (
                patch("agent_backends.codex_backend.subprocess.Popen", side_effect=fake_popen),
                patch("agent_backends.codex_backend.terminate_process_tree") as terminate,
            ):
                original_lang = os.environ.get("CHATBRIDGE_LANG")
                os.environ["CHATBRIDGE_LANG"] = "en-US"
                try:
                    result = backend.invoke(agent, "build the app", "", context)
                    written_session = session_file.read_text(encoding="utf-8")
                finally:
                    if original_lang is None:
                        os.environ.pop("CHATBRIDGE_LANG", None)
                    else:
                        os.environ["CHATBRIDGE_LANG"] = original_lang

        self.assertEqual("ok after recovery", result["output"])
        self.assertEqual("thread-timeout", result["session_id"])
        self.assertEqual("thread-timeout", written_session)
        self.assertEqual(2, len(calls))
        self.assertEqual(["codex", "exec", "resume"], calls[1][:3])
        self.assertIn("thread-timeout", calls[1])
        self.assertIn("You were interrupted unexpectedly", calls[1][-1])
        self.assertIn("10 minutes", calls[1][-1])
        self.assertIn("idle timeout", calls[1][-1])
        self.assertIn("build the app", calls[1][-1])
        self.assertTrue(any("automatically resuming" in item for item in progress))
        terminate.assert_called_once_with(9879)

    def test_codex_backend_does_not_timeout_while_stdout_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            workdir = temp_path / "workspace"
            workdir.mkdir(parents=True, exist_ok=True)
            session_dir = temp_path / "sessions"
            session_dir.mkdir(parents=True, exist_ok=True)

            agent = SimpleNamespace(
                id="main",
                name="Main",
                workdir=str(workdir),
                session_file=str(session_dir / "main.txt"),
                backend="codex",
                model="",
                prompt_prefix="",
            )
            reasoning: list[str] = []
            activities: list[dict[str, object]] = []
            context = BackendContext(
                codex_command="codex",
                claude_command="claude",
                opencode_command="opencode",
                session_dir=session_dir,
                creationflags=0,
                hub_task_timeout_seconds=1,
                on_reasoning=reasoning.append,
                on_activity=activities.append,
            )
            backend = CodexBackend()

            class StreamingStdout:
                def __iter__(self):
                    return self

                def __init__(self) -> None:
                    self.events = iter(
                        [
                            {"type": "thread.started", "thread_id": "thread-active"},
                            {"type": "item.completed", "item": {"type": "reasoning", "text": "inspect the current state"}},
                            {"type": "response.output_text.delta", "delta": "part 1"},
                            {"type": "response.output_text.delta", "delta": "part 2"},
                            {"type": "response.output_text.delta", "delta": "part 3"},
                        ]
                    )

                def __next__(self) -> str:
                    event = next(self.events)
                    time.sleep(0.4)
                    return json.dumps(event) + "\n"

            class StreamingProcess:
                pid = 9878
                stdout = StreamingStdout()
                stderr = iter([])
                returncode = 0

                def __init__(self, argv: list[str], *args: object, **kwargs: object) -> None:
                    output_path = Path(argv[argv.index("-o") + 1])
                    output_path.write_text("final output", encoding="utf-8")

                def poll(self) -> int | None:
                    return None

                def wait(self, timeout: float | None = None) -> int:
                    return 0

            with (
                patch("agent_backends.codex_backend.subprocess.Popen", side_effect=StreamingProcess),
                patch("agent_backends.codex_backend.terminate_process_tree") as terminate,
            ):
                result = backend.invoke(agent, "hello", "", context)

        self.assertEqual("final output", result["output"])
        self.assertEqual("thread-active", result["session_id"])
        self.assertEqual(["inspect the current state"], reasoning)
        self.assertEqual(["codex_reasoning"], [activity["event"] for activity in activities])
        self.assertEqual("inspect the current state", activities[0]["detail"])
        terminate.assert_not_called()

    def test_codex_backend_extracts_json_rpc_delta_progress(self) -> None:
        backend = CodexBackend()

        self.assertEqual(
            "streaming",
            backend._extract_text_delta({"method": "item/agentMessage/delta", "params": {"delta": "streaming"}}),
        )
        self.assertEqual(
            ("inspect state", False),
            backend._extract_reasoning_update(
                {"type": "item.completed", "item": {"type": "reasoning", "text": "inspect state"}}
            ),
        )
        self.assertEqual(
            ("inspect", True),
            backend._extract_reasoning_update(
                {"method": "item/reasoning/summaryTextDelta", "params": {"delta": "inspect"}}
            ),
        )

if __name__ == "__main__":
    unittest.main()
