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

from agent_hub import AgentConfig, HubConfig, MultiCodexHub, now_iso
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


class ContextCapturingBackend:
    key = "codex"

    def __init__(self) -> None:
        self.contexts = []

    def invoke(self, agent, prompt: str, session_name: str, context) -> dict[str, str]:
        self.contexts.append(context)
        return {"output": "done", "session_id": ""}


class AgentHubTimeTests(unittest.TestCase):
    def test_now_iso_is_utc_zulu(self) -> None:
        rendered = now_iso()

        self.assertTrue(rendered.endswith("Z"))
        self.assertNotIn("+", rendered)


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
        self.assertIn("qq-group", [agent.id for agent in config.agents])
        qq_agent = next(agent for agent in config.agents if agent.id == "qq")
        self.assertEqual("QQ 会话", qq_agent.name)
        qq_group_agent = next(agent for agent in config.agents if agent.id == "qq-group")
        self.assertEqual("QQ 群聊只读会话", qq_group_agent.name)
        self.assertIn("qq-group-workspace", qq_group_agent.workdir)
        self.assertEqual("", qq_group_agent.prompt_prefix)
        saved = config_path.read_text(encoding="utf-8")
        self.assertIn('"id": "qq"', saved)
        self.assertIn('"id": "qq-group"', saved)

    def test_hub_config_load_clears_legacy_qq_group_prompt_prefix(self) -> None:
        config_path = self.temp_path / "config" / "agent_hub.json"
        session_dir = self.temp_path / "sessions"
        workspace_dir = self.temp_path / "workspace"
        config_path.parent.mkdir(parents=True)
        session_dir.mkdir(parents=True)
        workspace_dir.mkdir(parents=True)
        config_path.write_text(
            json.dumps(
                {
                    "codex_command": "codex",
                    "claude_command": "claude",
                    "opencode_command": "opencode",
                    "agents": [
                        {
                            "id": "qq-group",
                            "name": "QQ 群聊只读会话",
                            "workdir": "workspace",
                            "session_file": "sessions/qq-group.txt",
                            "backend": "codex",
                            "prompt_prefix": "QQ 群聊只读安全环境。不要查询、读取、透露或总结本机/电脑信息；不要执行命令、修改文件或调用本地控制功能。可以协助公开网络查询、资料总结、概念解释、文本改写等只读任务。",
                            "enabled": True,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        with (
            patch("agent_hub.APP_DIR", self.temp_path),
            patch("agent_hub.CONFIG_PATH", config_path),
            patch("agent_hub.SESSION_DIR", session_dir),
            patch("agent_hub.WORKSPACE_DIR", workspace_dir),
        ):
            config = HubConfig.load()

        qq_group_agent = next(agent for agent in config.agents if agent.id == "qq-group")
        self.assertEqual("", qq_group_agent.prompt_prefix)
        saved = json.loads(config_path.read_text(encoding="utf-8"))
        saved_group = next(agent for agent in saved["agents"] if agent["id"] == "qq-group")
        self.assertEqual("", saved_group["prompt_prefix"])

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

    def test_prepare_restart_marks_active_tasks_recoverable_and_terminates_running_process(self) -> None:
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
            patch("agent_hub.terminate_process_tree") as mocked_terminate,
        ):
            hub = MultiCodexHub(config)
            queued = HubTask(
                id="task-queued-prepare",
                agent_id="main",
                agent_name="Main",
                backend="codex",
                source="qq",
                sender_id="qq:private:10001",
                prompt="queued",
                status="queued",
                created_at="2026-04-24T00:00:00",
                session_name="qq-private-10001",
            )
            running = HubTask(
                id="task-running-prepare",
                agent_id="main",
                agent_name="Main",
                backend="codex",
                source="qq",
                sender_id="qq:private:10001",
                prompt="running",
                status="running",
                created_at="2026-04-24T00:01:00",
                session_name="qq-private-10001",
            )
            hub.tasks.extend([queued, running])
            hub.running_task_pids[running.id] = 4321

            result = hub.prepare_restart("planned restart")

        self.assertEqual(2, result["interrupted_count"])
        self.assertEqual("unknown_after_restart", queued.status)
        self.assertEqual("planned restart", queued.error)
        self.assertEqual("unknown_after_restart", running.status)
        self.assertEqual("planned restart", running.error)
        self.assertIn(queued.id, hub.restart_prepared_task_ids)
        self.assertIn(running.id, hub.restart_prepared_task_ids)
        mocked_terminate.assert_called_once_with(4321)

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

    def test_stream_updates_keep_reasoning_and_live_output_separate(self) -> None:
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
            id="task-stream-001",
            agent_id="main",
            agent_name="Main",
            backend="codex",
            source="stream-web",
            sender_id="",
            prompt="hello",
            status="running",
            created_at="2026-04-24T00:00:00",
            session_name="default",
            workdir=str(workdir),
        )
        state_path = self.temp_path / "state" / "agent_hub_state.json"
        with (
            patch("agent_hub.STATE_PATH", state_path),
            patch("agent_hub.discover_external_agent_processes", return_value=[]),
        ):
            hub = MultiCodexHub(config)
            hub.tasks.append(task)
            hub._update_task_stream_text(task.id, "reasoning_text", "先检查项目")
            hub._update_task_stream_text(task.id, "live_output_text", "正在生成回答")

        saved_task = json.loads(state_path.read_text(encoding="utf-8"))["tasks"][0]
        self.assertEqual("先检查项目", task.reasoning_text)
        self.assertEqual("正在生成回答", task.live_output_text)
        self.assertEqual("正在生成回答", task.progress_text)
        self.assertEqual(2, task.progress_seq)
        self.assertEqual("先检查项目", saved_task["reasoning_text"])
        self.assertEqual("正在生成回答", saved_task["live_output_text"])

    def test_reasoning_callback_is_limited_to_web_tasks(self) -> None:
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
        wechat_task = HubTask(
            id="task-wechat-reasoning-001",
            agent_id="main",
            agent_name="Main",
            backend="codex",
            source="wechat",
            sender_id="sender-test",
            prompt="hello",
            status="running",
            created_at="2026-07-29T00:00:00",
            session_name="default",
            workdir=str(workdir),
        )
        web_task = HubTask(
            id="task-web-reasoning-001",
            agent_id="main",
            agent_name="Main",
            backend="codex",
            source="stream-web",
            sender_id="",
            prompt="hello",
            status="running",
            created_at="2026-07-29T00:00:00",
            session_name="default",
            workdir=str(workdir),
        )
        state_path = self.temp_path / "state" / "agent_hub_state.json"
        with (
            patch("agent_hub.STATE_PATH", state_path),
            patch("agent_hub.discover_external_agent_processes", return_value=[]),
        ):
            hub = MultiCodexHub(config)
            backend = ContextCapturingBackend()
            hub.backend_registry["codex"] = backend
            hub.tasks.extend([wechat_task, web_task])

            hub._invoke_backend(config.agents[0], wechat_task)
            hub._invoke_backend(config.agents[0], web_task)

            backend.contexts[0].on_reasoning("Inspecting repository")
            backend.contexts[0].on_progress("正常回答分片")
            backend.contexts[1].on_reasoning("Inspecting repository")

        self.assertEqual("", wechat_task.reasoning_text)
        self.assertEqual("正常回答分片", wechat_task.progress_text)
        self.assertEqual("Inspecting repository", web_task.reasoning_text)

    def test_command_activity_updates_existing_item_and_persists(self) -> None:
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
            id="task-command-001",
            agent_id="main",
            agent_name="Main",
            backend="codex",
            source="stream-web",
            sender_id="",
            prompt="run tests",
            status="running",
            created_at="2026-07-17T00:00:00",
            session_name="default",
            workdir=str(workdir),
        )
        state_path = self.temp_path / "state" / "agent_hub_state.json"
        with (
            patch("agent_hub.STATE_PATH", state_path),
            patch("agent_hub.discover_external_agent_processes", return_value=[]),
        ):
            hub = MultiCodexHub(config)
            hub.tasks.append(task)
            hub._update_task_activity(
                task.id,
                {
                    "id": "command-1",
                    "event": "codex_command",
                    "type": "info",
                    "detail": "pytest -q",
                    "metadata": {"command": "pytest -q", "status": "inProgress", "output": ""},
                },
            )
            hub._update_task_activity(
                task.id,
                {
                    "id": "command-1",
                    "event": "codex_command",
                    "type": "success",
                    "detail": "pytest -q",
                    "metadata": {"command": "pytest -q", "status": "completed", "output": "623 passed", "exit_code": 0},
                },
            )

        saved_task = json.loads(state_path.read_text(encoding="utf-8"))["tasks"][0]
        self.assertEqual(1, len(task.activity_items))
        self.assertEqual("success", task.activity_items[0]["type"])
        self.assertEqual("623 passed", task.activity_items[0]["metadata"]["output"])
        self.assertEqual(2, task.progress_seq)
        self.assertEqual(task.activity_items, saved_task["activity_items"])

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
            patch("agent_hub.broadcast_bridge_notice_by_kind") as mocked_broadcast,
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
            patch("agent_hub.broadcast_bridge_notice_by_kind") as mocked_broadcast,
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
