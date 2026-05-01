from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_hub import AgentConfig, HubConfig, MultiCodexHub
from core.state_models import HubTask


class SleepingBackend:
    key = "codex"

    def invoke(self, agent, prompt: str, session_name: str, context) -> dict[str, str]:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=agent.workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=context.creationflags,
            start_new_session=context.start_new_session,
            shell=False,
        )
        if context.on_process_started is not None:
            context.on_process_started(process.pid)
        _, stderr = process.communicate()
        if process.returncode != 0:
            raise RuntimeError(stderr.strip() or f"sleep process exited with code {process.returncode}")
        return {"output": "done", "session_id": ""}


class AgentHubCancellationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self._tempdir.name)

    def tearDown(self) -> None:
        self._tempdir.cleanup()

    def _wait_until(self, predicate, timeout: float = 10.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("timed out waiting for background task state")

    def test_cancel_running_task_marks_task_canceled(self) -> None:
        workdir = self.temp_path / "workspace"
        session_file = self.temp_path / "sessions" / "main.txt"
        workdir.mkdir(parents=True, exist_ok=True)
        session_file.parent.mkdir(parents=True, exist_ok=True)
        config = HubConfig(
            codex_command=sys.executable,
            claude_command="claude",
            opencode_command="opencode",
            agents=[AgentConfig("main", "Main", str(workdir), str(session_file), backend="codex")],
        )
        state_path = self.temp_path / "state" / "agent_hub_state.json"

        with (
            patch("agent_hub.STATE_PATH", state_path),
            patch("agent_hub.discover_external_agent_processes", return_value=[]),
        ):
            hub = MultiCodexHub(config)
            hub.backend_registry["codex"] = SleepingBackend()

            task_payload = hub.submit_task("main", "sleep please")
            task_id = str(task_payload["id"])

            self._wait_until(
                lambda: (
                    (task := hub._find_task(task_id)) is not None
                    and task.status == "running"
                    and int(hub.running_task_pids.get(task_id) or 0) > 0
                )
            )

            canceled_task = hub.cancel_task(task_id)
            self.assertEqual(task_id, canceled_task["id"])

            self._wait_until(lambda: str((hub.get_task(task_id) or {}).get("status") or "") == "canceled")

            final_task = hub.get_task(task_id) or {}
            self.assertEqual("canceled", final_task.get("status"))
            self.assertIn("canceled", str(final_task.get("error") or "").lower())
            self.assertNotIn(task_id, hub.running_task_pids)
            runtime = hub.runtimes["main"]
            self.assertEqual("idle", runtime.status)
            self.assertEqual(0, runtime.failure_count)

    def test_render_codex_status_runs_in_hub_context(self) -> None:
        workdir = self.temp_path / "workspace"
        session_file = self.temp_path / "sessions" / "main.txt"
        workdir.mkdir(parents=True, exist_ok=True)
        session_file.parent.mkdir(parents=True, exist_ok=True)
        config = HubConfig(
            codex_command="codex",
            claude_command="claude",
            opencode_command="opencode",
            agents=[AgentConfig("main", "Main", str(workdir), str(session_file), backend="codex")],
        )
        state_path = self.temp_path / "state" / "agent_hub_state.json"
        with (
            patch("agent_hub.STATE_PATH", state_path),
            patch("agent_hub.discover_external_agent_processes", return_value=[]),
            patch("agent_hub.query_codex_status_panel", return_value="OpenAI Codex v0.122.0") as mocked_query,
        ):
            hub = MultiCodexHub(config)
            status = hub.render_codex_status("main", "default", str(workdir))
        self.assertEqual("OpenAI Codex v0.122.0", status)
        mocked_query.assert_called_once()

    def test_get_task_context_left_percent_runs_in_hub_context(self) -> None:
        workdir = self.temp_path / "workspace"
        session_file = self.temp_path / "sessions" / "main.txt"
        workdir.mkdir(parents=True, exist_ok=True)
        session_file.parent.mkdir(parents=True, exist_ok=True)
        config = HubConfig(
            codex_command="codex",
            claude_command="claude",
            opencode_command="opencode",
            agents=[AgentConfig("main", "Main", str(workdir), str(session_file), backend="codex")],
        )
        state_path = self.temp_path / "state" / "agent_hub_state.json"
        task = HubTask(
            id="task-ctx-001",
            agent_id="main",
            agent_name="Main",
            backend="codex",
            source="wechat",
            sender_id="sender-test",
            prompt="hello",
            status="running",
            created_at="2026-04-24T00:00:00",
            session_name="default",
            workdir=str(workdir),
        )
        with (
            patch("agent_hub.STATE_PATH", state_path),
            patch("agent_hub.discover_external_agent_processes", return_value=[]),
            patch("agent_hub.query_codex_context_left_percent", return_value=18) as mocked_query,
        ):
            hub = MultiCodexHub(config)
            hub.tasks.append(task)
            percent = hub.get_task_context_left_percent("task-ctx-001")
        self.assertEqual(18, percent)
        self.assertEqual(18, task.context_left_percent)
        mocked_query.assert_called_once()

    def test_request_task_context_left_refresh_returns_cached_value_and_runs_background_lookup(self) -> None:
        workdir = self.temp_path / "workspace"
        session_file = self.temp_path / "sessions" / "main.txt"
        workdir.mkdir(parents=True, exist_ok=True)
        session_file.parent.mkdir(parents=True, exist_ok=True)
        config = HubConfig(
            codex_command="codex",
            claude_command="claude",
            opencode_command="opencode",
            agents=[AgentConfig("main", "Main", str(workdir), str(session_file), backend="codex")],
        )
        state_path = self.temp_path / "state" / "agent_hub_state.json"
        task = HubTask(
            id="task-ctx-refresh-001",
            agent_id="main",
            agent_name="Main",
            backend="codex",
            source="wechat",
            sender_id="sender-test",
            prompt="hello",
            status="running",
            created_at="2026-04-24T00:00:00",
            session_name="default",
            workdir=str(workdir),
            context_left_percent=42,
        )
        started_threads: list[tuple[object, tuple[object, ...]]] = []

        class ImmediateThread:
            def __init__(self, target, args=(), daemon=None) -> None:
                self.target = target
                self.args = args
                self.daemon = daemon

            def start(self) -> None:
                target_name = getattr(self.target, "__name__", "")
                if target_name in {"_worker", "_refresh_external_agent_processes_worker"}:
                    return
                started_threads.append((self.target, self.args))
                self.target(*self.args)

        with (
            patch("agent_hub.STATE_PATH", state_path),
            patch("agent_hub.discover_external_agent_processes", return_value=[]),
            patch("agent_hub.threading.Thread", ImmediateThread),
            patch("agent_hub.query_codex_context_left_percent", return_value=17) as mocked_query,
        ):
            hub = MultiCodexHub(config)
            hub.tasks.append(task)
            percent = hub.request_task_context_left_refresh("task-ctx-refresh-001")

        self.assertEqual(42, percent)
        self.assertEqual(17, task.context_left_percent)
        self.assertEqual(1, len(started_threads))
        mocked_query.assert_called_once()

    def test_save_state_does_not_scan_external_agent_processes(self) -> None:
        workdir = self.temp_path / "workspace"
        session_file = self.temp_path / "sessions" / "main.txt"
        workdir.mkdir(parents=True, exist_ok=True)
        session_file.parent.mkdir(parents=True, exist_ok=True)
        config = HubConfig(
            codex_command="codex",
            claude_command="claude",
            opencode_command="opencode",
            agents=[AgentConfig("main", "Main", str(workdir), str(session_file), backend="codex")],
        )
        state_path = self.temp_path / "state" / "agent_hub_state.json"
        with (
            patch("agent_hub.STATE_PATH", state_path),
            patch("agent_hub.discover_external_agent_processes") as mocked_discover,
        ):
            hub = MultiCodexHub(config)
            hub._save_state()

        mocked_discover.assert_not_called()

    def test_refresh_external_agent_processes_scans_on_explicit_request(self) -> None:
        workdir = self.temp_path / "workspace"
        session_file = self.temp_path / "sessions" / "main.txt"
        workdir.mkdir(parents=True, exist_ok=True)
        session_file.parent.mkdir(parents=True, exist_ok=True)
        config = HubConfig(
            codex_command="codex",
            claude_command="claude",
            opencode_command="opencode",
            agents=[AgentConfig("main", "Main", str(workdir), str(session_file), backend="codex")],
        )
        state_path = self.temp_path / "state" / "agent_hub_state.json"
        process = SimpleNamespace(to_dict=lambda: {"pid": 123, "backend": "codex"})
        with (
            patch("agent_hub.STATE_PATH", state_path),
            patch("agent_hub.discover_external_agent_processes", return_value=[process]) as mocked_discover,
        ):
            hub = MultiCodexHub(config)
            snapshot = hub.refresh_external_agent_processes()

        self.assertEqual([{"pid": 123, "backend": "codex"}], snapshot)
        mocked_discover.assert_called_once()


if __name__ == "__main__":
    unittest.main()
