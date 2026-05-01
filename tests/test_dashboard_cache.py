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

    def tearDown(self) -> None:
        dashboard._RUNTIME_CACHE.clear()

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


if __name__ == "__main__":
    unittest.main()
