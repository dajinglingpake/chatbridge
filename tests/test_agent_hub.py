from __future__ import annotations

import json
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

    def test_hub_config_load_adds_dedicated_qq_agent(self) -> None:
        config_path = self.temp_path / "config" / "agent_hub.json"
        session_dir = self.temp_path / "sessions"
        workspace_dir = self.temp_path / "workspace"
        config_path.parent.mkdir(parents=True)
        session_dir.mkdir(parents=True)
        workspace_dir.mkdir(parents=True)
        config_path.write_text(
            '{"codex_command":"codex","claude_command":"claude","opencode_command":"opencode","agents":[{"id":"main","name":"Main","workdir":"workspace","session_file":"sessions/main.txt","backend":"codex","enabled":true}]}',
            encoding="utf-8",
        )

        with (
            patch("agent_hub.APP_DIR", self.temp_path),
            patch("agent_hub.CONFIG_PATH", config_path),
            patch("agent_hub.SESSION_DIR", session_dir),
            patch("agent_hub.WORKSPACE_DIR", workspace_dir),
        ):
            config = HubConfig.load()

        self.assertIn("main", [agent.id for agent in config.agents])
        self.assertIn("qq", [agent.id for agent in config.agents])
        qq_agent = next(agent for agent in config.agents if agent.id == "qq")
        self.assertEqual("QQ 会话", qq_agent.name)
        saved = config_path.read_text(encoding="utf-8")
        self.assertIn('"id": "qq"', saved)

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
            self._wait_until(
                lambda: (
                    task_id not in hub.running_task_pids
                    and hub.runtimes["main"].status == "idle"
                    and hub.runtimes["main"].queue_size == 0
                )
            )

            final_task = hub.get_task(task_id) or {}
            self.assertEqual("canceled", final_task.get("status"))
            self.assertIn("canceled", str(final_task.get("error") or "").lower())
            self.assertNotIn(task_id, hub.running_task_pids)
            runtime = hub.runtimes["main"]
            self.assertEqual("idle", runtime.status)
            self.assertEqual(0, runtime.failure_count)
            hub.queues["main"].join()

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

    def test_restore_marks_active_tasks_unknown_after_restart(self) -> None:
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
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "tasks": [
                        {
                            "id": "task-queued-restore",
                            "agent_id": "main",
                            "agent_name": "Main",
                            "backend": "codex",
                            "source": "wechat",
                            "sender_id": "sender-test",
                            "prompt": "queued",
                            "status": "queued",
                            "created_at": "2026-04-24T00:00:00",
                            "session_name": "default",
                            "workdir": str(workdir),
                        },
                        {
                            "id": "task-running-restore",
                            "agent_id": "main",
                            "agent_name": "Main",
                            "backend": "codex",
                            "source": "wechat",
                            "sender_id": "sender-test",
                            "prompt": "running",
                            "status": "running",
                            "created_at": "2026-04-24T00:01:00",
                            "session_name": "default",
                            "workdir": str(workdir),
                        },
                    ],
                    "agents": [
                        {
                            "id": "main",
                            "runtime": {
                                "status": "running",
                                "queue_size": 2,
                                "updated_at": "2026-04-24T00:02:00",
                            },
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        with (
            patch("agent_hub.STATE_PATH", state_path),
            patch("agent_hub.discover_external_agent_processes", return_value=[]),
        ):
            hub = MultiCodexHub(config)

        queued = hub.get_task("task-queued-restore") or {}
        running = hub.get_task("task-running-restore") or {}
        self.assertEqual("unknown_after_restart", queued.get("status"))
        self.assertIn("before this task could run", str(queued.get("error") or ""))
        self.assertEqual("unknown_after_restart", running.get("status"))
        self.assertIn("while this task was running", str(running.get("error") or ""))
        runtime = hub.runtimes["main"]
        self.assertEqual("idle", runtime.status)
        self.assertEqual(0, runtime.queue_size)

    def test_progress_update_pushes_task_update_to_bridge_ipc(self) -> None:
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
            id="task-push-001",
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
            patch("agent_hub.create_bridge_request") as mocked_push,
        ):
            hub = MultiCodexHub(config)
            hub.tasks.append(task)
            hub._update_task_progress("task-push-001", "正在处理")

        mocked_push.assert_called_once()
        action, payload = mocked_push.call_args.args
        self.assertEqual("task_update", action)
        self.assertEqual("progress", payload["event"])
        self.assertEqual("task-push-001", payload["task"]["id"])
        self.assertEqual("正在处理", payload["task"]["progress_text"])
        self.assertEqual("wechat", mocked_push.call_args.kwargs["channel"])

    def test_qq_progress_update_pushes_to_qq_bridge_channel(self) -> None:
        workdir = self.temp_path / "workspace"
        session_file = self.temp_path / "sessions" / "main.txt"
        workdir.mkdir(parents=True, exist_ok=True)
        session_file.parent.mkdir(parents=True, exist_ok=True)
        config = HubConfig(
            codex_command="codex",
            claude_command="claude",
            opencode_command="opencode",
            agents=[AgentConfig("qq", "QQ", str(workdir), str(session_file), backend="codex")],
        )
        state_path = self.temp_path / "state" / "agent_hub_state.json"
        task = HubTask(
            id="task-qq-push-001",
            agent_id="qq",
            agent_name="QQ",
            backend="codex",
            source="qq",
            sender_id="qq:private:10001",
            prompt="hello",
            status="running",
            created_at="2026-06-26T00:00:00",
            session_name="qq-private-10001",
            workdir=str(workdir),
        )
        with (
            patch("agent_hub.STATE_PATH", state_path),
            patch("agent_hub.discover_external_agent_processes", return_value=[]),
            patch("agent_hub.create_bridge_request") as mocked_push,
        ):
            hub = MultiCodexHub(config)
            hub.tasks.append(task)
            hub._update_task_progress("task-qq-push-001", "QQ 正在处理")

        mocked_push.assert_called_once()
        action, payload = mocked_push.call_args.args
        self.assertEqual("task_update", action)
        self.assertEqual("task-qq-push-001", payload["task"]["id"])
        self.assertEqual("qq", mocked_push.call_args.kwargs["channel"])

    def test_qq_task_result_does_not_broadcast_weixin_notice(self) -> None:
        workdir = self.temp_path / "workspace"
        session_file = self.temp_path / "sessions" / "main.txt"
        workdir.mkdir(parents=True, exist_ok=True)
        session_file.parent.mkdir(parents=True, exist_ok=True)
        config = HubConfig(
            codex_command="codex",
            claude_command="claude",
            opencode_command="opencode",
            agents=[AgentConfig("qq", "QQ", str(workdir), str(session_file), backend="codex")],
        )
        task = HubTask(
            id="task-qq-notify-001",
            agent_id="qq",
            agent_name="QQ",
            backend="codex",
            source="qq",
            sender_id="qq:private:10001",
            prompt="hello",
            status="succeeded",
            created_at="2026-06-26T00:00:00",
            session_name="qq-private-10001",
            workdir=str(workdir),
            output="done",
        )
        with (
            patch("agent_hub.STATE_PATH", self.temp_path / "state" / "agent_hub_state.json"),
            patch("agent_hub.discover_external_agent_processes", return_value=[]),
            patch("agent_hub.broadcast_weixin_notice_by_kind") as mocked_broadcast,
        ):
            hub = MultiCodexHub(config)
            hub._notify_task_result(task, succeeded=True)
            hub._notify_task_canceled(task)

        mocked_broadcast.assert_not_called()

    def test_desktop_task_result_still_broadcasts_weixin_notice(self) -> None:
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
        task = HubTask(
            id="task-desktop-notify-001",
            agent_id="main",
            agent_name="Main",
            backend="codex",
            source="desktop",
            sender_id="",
            prompt="hello",
            status="succeeded",
            created_at="2026-06-26T00:00:00",
            session_name="default",
            workdir=str(workdir),
            output="done",
        )
        with (
            patch("agent_hub.STATE_PATH", self.temp_path / "state" / "agent_hub_state.json"),
            patch("agent_hub.discover_external_agent_processes", return_value=[]),
            patch("agent_hub.broadcast_weixin_notice_by_kind") as mocked_broadcast,
        ):
            hub = MultiCodexHub(config)
            hub._notify_task_result(task, succeeded=True)

        mocked_broadcast.assert_called_once()

    def test_progress_update_still_succeeds_when_bridge_push_fails(self) -> None:
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
            id="task-push-fail-001",
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
            patch("agent_hub.create_bridge_request", side_effect=RuntimeError("bridge unavailable")),
        ):
            hub = MultiCodexHub(config)
            hub.tasks.append(task)
            hub._update_task_progress("task-push-fail-001", "仍然继续")

        self.assertEqual("仍然继续", task.progress_text)
        self.assertEqual(1, task.progress_seq)

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
