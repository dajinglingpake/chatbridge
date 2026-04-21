from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_backends.base import BackendContext, McpServerConfig
from agent_backends.codex_backend import CodexBackend
from agent_hub import APP_DIR, AgentConfig, HubConfig, MultiCodexHub, load_manager_prompt_template
from core.manager_agent_runtime import ChatBridgeManagerRuntime
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


class RecordingManagerRuntime:
    def __init__(self) -> None:
        self.last_sender_id = ""
        self.last_prompt = ""
        self.last_instructions = ""
        self.last_model = ""
        self.last_mcp = None

    def invoke(self, *, sender_id: str, prompt: str, instructions: str, model: str, mcp_config, on_progress=None) -> dict[str, str]:
        self.last_sender_id = sender_id
        self.last_prompt = prompt
        self.last_instructions = instructions
        self.last_model = model
        self.last_mcp = mcp_config
        return {"output": "ok", "session_id": "mgr-thread-1"}


class ManagementAgentHubTests(unittest.TestCase):
    def test_load_manager_prompt_template_prefers_prompt_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            prompt_path = Path(temp_dir) / "chatbridge_manager_prompt.txt"
            prompt_path.write_text("manager prompt for {sender_id}", encoding="utf-8")
            with patch("agent_hub.CHATBRIDGE_MANAGER_PROMPT_PATH", prompt_path):
                self.assertEqual("manager prompt for {sender_id}", load_manager_prompt_template())

    def test_wechat_manager_task_builds_manager_prompt_and_mcp(self) -> None:
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
            manager_runtime = RecordingManagerRuntime()
            with (
                patch("agent_hub.STATE_PATH", temp_path / "state" / "agent_hub_state.json"),
                patch("agent_hub.discover_external_agent_processes", return_value=[]),
            ):
                hub = MultiCodexHub(config)
                hub.backend_registry["codex"] = backend
                hub.manager_runtime = manager_runtime
                task = HubTask(
                    id="task-mgr-001",
                    agent_id="main",
                    agent_name="Main",
                    backend="codex",
                    source="wechat-manager",
                    sender_id="sender-test",
                    prompt="列出所有会话",
                    status="queued",
                    created_at="2026-04-20T20:00:00",
                    session_name="__manager__-sender-test",
                )

                result = hub._invoke_backend(config.agents[0], task)

            self.assertEqual("ok", result["output"])
            self.assertEqual("mgr-thread-1", result["session_id"])
            self.assertEqual("sender-test", manager_runtime.last_sender_id)
            self.assertEqual("列出所有会话", manager_runtime.last_prompt)
            self.assertIsNotNone(manager_runtime.last_mcp)
            self.assertEqual("chatbridge_manager", manager_runtime.last_mcp.name)
            self.assertIn("--trusted-internal-manager", manager_runtime.last_mcp.args)
            self.assertIn("优先直接复述 MCP 返回里的 summary_lines", manager_runtime.last_instructions)
            self.assertEqual("", backend.last_prompt)

    def test_wechat_manager_task_passes_bridge_state_overrides_to_mcp(self) -> None:
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
            manager_runtime = RecordingManagerRuntime()
            conversation_path = temp_path / ".runtime" / "state" / "weixin_conversations.json"
            event_log_path = temp_path / ".runtime" / "logs" / "weixin_bridge_events.jsonl"
            manager_state_path = temp_path / ".runtime" / "state" / "chatbridge_manager_state.json"
            with (
                patch("agent_hub.STATE_PATH", temp_path / "state" / "agent_hub_state.json"),
                patch("agent_hub.discover_external_agent_processes", return_value=[]),
            ):
                hub = MultiCodexHub(config)
                hub.backend_registry["codex"] = backend
                hub.manager_runtime = manager_runtime
                task = HubTask(
                    id="task-mgr-002",
                    agent_id="main",
                    agent_name="Main",
                    backend="codex",
                    source="wechat-manager",
                    sender_id="sender-test",
                    prompt="列出所有会话",
                    status="queued",
                    created_at="2026-04-20T20:00:00",
                    session_name="__manager__-sender-test",
                    bridge_conversations_path=str(conversation_path),
                    bridge_event_log_path=str(event_log_path),
                    manager_state_path=str(manager_state_path),
                )

                hub._invoke_backend(config.agents[0], task)

            self.assertIsNotNone(manager_runtime.last_mcp)
            self.assertIn("--bridge-conversations-path", manager_runtime.last_mcp.args)
            self.assertIn(str(conversation_path), manager_runtime.last_mcp.args)
            self.assertIn("--bridge-event-log-path", manager_runtime.last_mcp.args)
            self.assertIn(str(event_log_path), manager_runtime.last_mcp.args)
            self.assertIn("--manager-state-path", manager_runtime.last_mcp.args)
            self.assertIn(str(manager_state_path), manager_runtime.last_mcp.args)


class ManagementAgentCodexBackendTests(unittest.TestCase):
    def test_codex_backend_injects_chatbridge_mcp_overrides(self) -> None:
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
                chatbridge_mcp=McpServerConfig(
                    name="chatbridge_manager",
                    command="python3",
                    args=["/tmp/chatbridge_mcp_server.py", "--trusted-internal-manager"],
                ),
            )
            backend = CodexBackend()

            class FakeProcess:
                def __init__(self, argv: list[str]) -> None:
                    self.argv = argv
                    self.pid = 4321
                    self.stdout = iter(['{"type":"thread.started","thread_id":"thread-1"}\n'])
                    self.stderr = iter([])

                def wait(self) -> int:
                    return 0

            def fake_popen(argv: list[str], **kwargs):
                output_path.write_text("ok", encoding="utf-8")
                self.assertIn('mcp_servers.chatbridge_manager.command="python3"', argv)
                self.assertIn('mcp_servers.chatbridge_manager.args=["/tmp/chatbridge_mcp_server.py", "--trusted-internal-manager"]', argv)
                return FakeProcess(argv)

            with (
                patch("agent_backends.codex_backend.tempfile.gettempdir", return_value=str(temp_path)),
                patch("agent_backends.codex_backend.uuid.uuid4", return_value=SimpleNamespace(hex="fixed")),
                patch("agent_backends.codex_backend.subprocess.Popen", side_effect=fake_popen),
            ):
                result = backend.invoke(agent, "hello", "", context)

            self.assertEqual("ok", result["output"])
            self.assertEqual("thread-1", result["session_id"])


class ManagementAgentRuntimeTests(unittest.TestCase):
    def test_extract_progress_text_ignores_real_mcp_tool_events(self) -> None:
        runtime = ChatBridgeManagerRuntime(codex_command="codex")
        started = runtime._extract_progress_text(
            {
                "method": "item/started",
                "params": {
                    "item": {
                        "type": "mcpToolCall",
                        "tool": "get_management_snapshot",
                        "status": "inProgress",
                    }
                },
            }
        )
        completed = runtime._extract_progress_text(
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "mcpToolCall",
                        "tool": "get_management_snapshot",
                        "status": "completed",
                    }
                },
            }
        )
        self.assertEqual("", started)
        self.assertEqual("", completed)

    def test_extract_progress_text_accumulates_agent_message_delta(self) -> None:
        runtime = ChatBridgeManagerRuntime(codex_command="codex")
        buffers: dict[str, str] = {}
        first = runtime._extract_progress_text(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "itemId": "msg-1",
                    "delta": "正在",
                },
            },
            message_buffers=buffers,
        )
        second = runtime._extract_progress_text(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "itemId": "msg-1",
                    "delta": "整理会话列表。",
                },
            },
            message_buffers=buffers,
        )
        self.assertEqual("", first)
        self.assertEqual("正在整理会话列表。", second)


if __name__ == "__main__":
    unittest.main()
