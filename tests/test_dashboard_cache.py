from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import core.dashboard as dashboard
from core.state_models import CheckSnapshot


class DashboardCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        dashboard._RUNTIME_CACHE.clear()
        dashboard._STATE_FILE_CACHE.clear()

    def tearDown(self) -> None:
        dashboard._RUNTIME_CACHE.clear()
        dashboard._STATE_FILE_CACHE.clear()

    def test_load_dashboard_reads_cached_checks_without_collecting(self) -> None:
        dashboard._RUNTIME_CACHE["checks:full"] = dashboard.RuntimeCacheEntry(
            cached_at=0.0,
            payload={"stale": CheckSnapshot(key="stale", label="Stale", ok=True, detail="cached")},
        )

        with (
            patch("core.dashboard.collect_check_step") as mocked_collect,
            patch("core.dashboard.get_runtime_snapshot") as mocked_snapshot,
            patch("core.dashboard.BridgeConfig.load") as mocked_config,
            patch("core.dashboard._read_hub_state") as mocked_hub,
            patch("core.dashboard._read_bridge_state") as mocked_bridge,
        ):
            mocked_snapshot.return_value = SimpleNamespace(hub_pid=None, bridge_pid=None)
            mocked_config.return_value = SimpleNamespace(active_account_id="", default_backend="codex")
            mocked_hub.return_value = SimpleNamespace(external_agent_processes=[])
            mocked_bridge.return_value = SimpleNamespace()
            state = dashboard.load_dashboard_state(Path("."), "diagnostics")

        self.assertEqual({"stale"}, set(state.checks.keys()))
        mocked_collect.assert_not_called()

    def test_load_dashboard_reuses_runtime_snapshot_briefly(self) -> None:
        first_snapshot = SimpleNamespace(hub_pid=1, bridge_pid=2, log_dir="logs")
        second_snapshot = SimpleNamespace(hub_pid=3, bridge_pid=4, log_dir="logs")

        with (
            patch("core.dashboard.time.monotonic", side_effect=[100.0, 100.5, 101.0]),
            patch("core.dashboard.get_runtime_snapshot", side_effect=[first_snapshot, second_snapshot]) as mocked_snapshot,
            patch("core.dashboard.BridgeConfig.load") as mocked_config,
            patch("core.dashboard._read_hub_state") as mocked_hub,
            patch("core.dashboard._read_bridge_state") as mocked_bridge,
        ):
            mocked_config.return_value = SimpleNamespace(active_account_id="", default_backend="codex")
            mocked_hub.return_value = SimpleNamespace(external_agent_processes=[])
            mocked_bridge.return_value = SimpleNamespace()
            first = dashboard.load_dashboard_state(Path("."), "home")
            second = dashboard.load_dashboard_state(Path("."), "sessions", load_bridge_conversations=False)
            third = dashboard.load_dashboard_state(Path("."), "mobile")

        self.assertIs(first.snapshot, first_snapshot)
        self.assertIs(second.snapshot, second_snapshot)
        self.assertIs(third.snapshot, second_snapshot)
        self.assertEqual(2, mocked_snapshot.call_count)

    def test_refresh_dashboard_cache_can_force_runtime_snapshot_refresh(self) -> None:
        snapshot = SimpleNamespace(hub_pid=1, bridge_pid=2, log_dir="logs")

        with patch("core.dashboard.get_runtime_snapshot", return_value=snapshot) as mocked_snapshot:
            dashboard.refresh_dashboard_cache(Path("."), "runtime")

        cached = dashboard._RUNTIME_CACHE["runtime_snapshot:False:True:True"].payload
        self.assertIs(snapshot, cached)
        mocked_snapshot.assert_called_once_with(
            include_agent_processes=False,
            include_qq_login_status=True,
            discover_missing_processes=True,
        )

    def test_sessions_dashboard_uses_light_runtime_snapshot(self) -> None:
        snapshot = SimpleNamespace(hub_pid=1, bridge_pid=None, log_dir="logs")

        with (
            patch("core.dashboard.get_runtime_snapshot", return_value=snapshot) as mocked_snapshot,
            patch("core.dashboard.BridgeConfig.load") as mocked_config,
            patch("core.dashboard._read_hub_state") as mocked_hub,
            patch("core.dashboard._read_bridge_state") as mocked_bridge,
        ):
            mocked_config.return_value = SimpleNamespace(active_account_id="", default_backend="codex")
            mocked_hub.return_value = SimpleNamespace(external_agent_processes=[])
            mocked_bridge.return_value = SimpleNamespace()
            dashboard.load_dashboard_state(Path("."), "sessions", load_bridge_conversations=False)

        mocked_snapshot.assert_called_once_with(
            include_agent_processes=False,
            include_qq_login_status=False,
            discover_missing_processes=False,
        )

    def test_refresh_logs_reuses_runtime_snapshot_cache_briefly(self) -> None:
        snapshot = SimpleNamespace(
            hub_pid=None,
            bridge_pid=None,
            onebot_runtime_pid=None,
            qq_bridge_pid=None,
            log_dir="logs",
        )

        with (
            patch("core.dashboard.time.monotonic", side_effect=[100.0, 100.1, 100.5, 100.6]),
            patch("core.dashboard.BridgeConfig.load", return_value=SimpleNamespace()),
            patch("core.dashboard.get_runtime_snapshot", return_value=snapshot) as mocked_snapshot,
            patch("core.dashboard._load_logs", return_value={"hub_out": "ok"}) as mocked_logs,
        ):
            dashboard.refresh_dashboard_cache(Path("."), "logs")
            dashboard.refresh_dashboard_cache(Path("."), "logs")

        self.assertEqual(1, mocked_snapshot.call_count)
        self.assertEqual(2, mocked_logs.call_count)

    def test_refresh_dashboard_cache_collects_full_checks_on_explicit_request(self) -> None:
        sequence = ["step-a", "step-b"]
        step_results = {
            "step-a": [SimpleNamespace(key="python", label="Python", ok=True, detail="3.11.9")],
            "step-b": [SimpleNamespace(key="node", label="Node.js", ok=False, detail="missing")],
        }

        with (
            patch("core.dashboard.BridgeConfig.load", return_value=object()),
            patch("core.dashboard.get_full_check_sequence", return_value=sequence),
            patch("core.dashboard.collect_check_step", side_effect=lambda step, *_: step_results[step]),
        ):
            dashboard.refresh_dashboard_cache(Path("."), "checks_full")

        cached = dashboard._RUNTIME_CACHE["checks:full"].payload
        self.assertEqual({"python", "node"}, set(cached.keys()))
        self.assertIsInstance(cached["python"], CheckSnapshot)

    def test_tail_text_hides_stale_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "service.err.log"
            path.write_text("old traceback\n", encoding="utf-8")
            os.utime(path, (100.0, 100.0))

            self.assertEqual("(empty)", dashboard.tail_text(path, stale_before=101.0))

    def test_tail_text_suppresses_expected_timeout_noise(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bridge.out.log"
            path.write_text(
                "startup\n"
                "[bridge] poll error: The read operation timed out\n"
                "real event\n",
                encoding="utf-8",
            )

            self.assertEqual("startup\nreal event", dashboard.tail_text(path, suppress_expected_noise=True))

    def test_tail_text_starts_at_last_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bridge.out.log"
            path.write_text(
                "old run\n"
                "Weixin Hub Bridge started at 2026-04-28T10:00:00\n"
                "old error\n"
                "Weixin Hub Bridge started at 2026-04-28T11:00:00\n"
                "current event\n",
                encoding="utf-8",
            )

            self.assertEqual(
                "Weixin Hub Bridge started at 2026-04-28T11:00:00\ncurrent event",
                dashboard.tail_text(path, start_marker="Weixin Hub Bridge started at"),
            )


    def test_tail_text_reads_large_log_from_tail_without_full_read_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "large.out.log"
            path.write_text(
                "".join(f"old line {index}\n" for index in range(20000))
                + "current a\n"
                + "current b\n",
                encoding="utf-8",
            )

            with patch.object(Path, "read_text", side_effect=AssertionError("full file read")):
                self.assertEqual("current a\ncurrent b", dashboard.tail_text(path, max_lines=2))

    def test_hub_state_reuses_cached_parse_when_file_signature_is_unchanged(self) -> None:
        path = Path("hub_state.json")
        config = SimpleNamespace(default_backend="codex")

        with (
            patch("core.dashboard._file_signature", return_value=(10, 100)),
            patch(
                "core.dashboard.read_json",
                return_value={
                    "tasks": [
                        {
                            "id": "task-1",
                            "agent_id": "main",
                            "backend": "codex",
                            "created_at": "2026-07-05T10:00:00",
                        }
                    ]
                },
            ) as mocked_read,
        ):
            first = dashboard._read_hub_state(path, config)
            second = dashboard._read_hub_state(path, config)

        self.assertIs(first, second)
        self.assertEqual(1, mocked_read.call_count)
        self.assertEqual(["task-1"], [task.id for task in first.tasks])

    def test_hub_state_light_mode_does_not_materialize_task_text(self) -> None:
        class ExplodingText:
            def __str__(self) -> str:
                raise AssertionError("task text was materialized")

        path = Path("hub_state.json")
        config = SimpleNamespace(default_backend="codex")

        with (
            patch("core.dashboard._file_signature", return_value=(12, 120)),
            patch(
                "core.dashboard.read_json",
                return_value={
                    "tasks": [
                        {
                            "id": "task-1",
                            "agent_id": "main",
                            "created_at": "2026-07-05T10:00:00",
                            "prompt": ExplodingText(),
                            "output": ExplodingText(),
                            "error": ExplodingText(),
                            "progress_text": ExplodingText(),
                        }
                    ]
                },
            ),
        ):
            state = dashboard._read_hub_state(path, config, include_task_text=False)

        self.assertEqual("task-1", state.tasks[0].id)
        self.assertEqual("", state.tasks[0].prompt)
        self.assertEqual("", state.tasks[0].output)
        self.assertEqual("", state.tasks[0].error)
        self.assertEqual("", state.tasks[0].progress_text)

    def test_hub_state_can_skip_task_objects_for_non_task_pages(self) -> None:
        path = Path("hub_state.json")
        config = SimpleNamespace(default_backend="codex")

        with (
            patch("core.dashboard._file_signature", return_value=(12, 121)),
            patch(
                "core.dashboard.read_json",
                return_value={
                    "agents": [{"id": "main", "name": "Main"}],
                    "tasks": [{"id": "task-1", "agent_id": "main", "created_at": "2026-07-05T10:00:00"}],
                },
            ),
            patch("core.state_models.HubTask.from_dict", side_effect=AssertionError("tasks should not be parsed")),
        ):
            state = dashboard._read_hub_state(path, config, include_tasks=False)

        self.assertEqual(["main"], [agent.id for agent in state.agents])
        self.assertEqual([], state.tasks)

    def test_hub_state_cache_separates_light_and_full_task_text(self) -> None:
        path = Path("hub_state.json")
        config = SimpleNamespace(default_backend="codex")

        with (
            patch("core.dashboard._file_signature", return_value=(13, 130)),
            patch(
                "core.dashboard.read_json",
                return_value={
                    "tasks": [
                        {
                            "id": "task-1",
                            "agent_id": "main",
                            "created_at": "2026-07-05T10:00:00",
                            "prompt": "BIG_PROMPT",
                            "output": "BIG_OUTPUT",
                        }
                    ]
                },
            ) as mocked_read,
        ):
            light = dashboard._read_hub_state(path, config, include_task_text=False)
            full = dashboard._read_hub_state(path, config, include_task_text=True)

        self.assertEqual(2, mocked_read.call_count)
        self.assertEqual("", light.tasks[0].prompt)
        self.assertEqual("BIG_PROMPT", full.tasks[0].prompt)
        self.assertEqual("BIG_OUTPUT", full.tasks[0].output)

    def test_hub_state_rereads_when_file_signature_changes(self) -> None:
        path = Path("hub_state.json")
        config = SimpleNamespace(default_backend="codex")
        payloads = [
            {"tasks": [{"id": "task-old", "agent_id": "main", "created_at": "2026-07-05T10:00:00"}]},
            {"tasks": [{"id": "task-new", "agent_id": "main", "created_at": "2026-07-05T10:01:00"}]},
        ]

        with (
            patch("core.dashboard._file_signature", side_effect=[(10, 100), (11, 100)]),
            patch("core.dashboard.read_json", side_effect=payloads) as mocked_read,
        ):
            first = dashboard._read_hub_state(path, config)
            second = dashboard._read_hub_state(path, config)

        self.assertEqual(2, mocked_read.call_count)
        self.assertEqual(["task-old"], [task.id for task in first.tasks])
        self.assertEqual(["task-new"], [task.id for task in second.tasks])

    def test_bridge_state_reuses_cached_parse_when_file_signature_is_unchanged(self) -> None:
        path = Path("bridge_state.json")

        with (
            patch("core.dashboard._file_signature", return_value=(20, 200)),
            patch("core.dashboard.read_json", return_value={"started_at": "2026-07-05T10:00:00"}) as mocked_read,
        ):
            first = dashboard._read_bridge_state(path)
            second = dashboard._read_bridge_state(path)

        self.assertIs(first, second)
        self.assertEqual(1, mocked_read.call_count)
        self.assertEqual("2026-07-05T10:00:00", first.started_at)

if __name__ == "__main__":
    unittest.main()
