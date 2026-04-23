from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_backends.base import BackendContext, McpServerConfig
from agent_backends.codex_backend import CodexBackend
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

class McpServerInjectionTests(unittest.TestCase):
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
                self.assertIn('mcp_servers.operations.command="python3"', argv)
                self.assertIn('mcp_servers.operations.args=["/tmp/operations_server.py"]', argv)
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
                self.assertIn('-a', argv)
                self.assertIn('never', argv)
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

if __name__ == "__main__":
    unittest.main()
