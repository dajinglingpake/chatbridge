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

    def test_get_progressive_full_checks_accumulates_results(self) -> None:
        sequence = ["step-a", "step-b"]
        step_results = {
            "step-a": [SimpleNamespace(key="python", label="Python", ok=True, detail="3.11.9")],
            "step-b": [SimpleNamespace(key="node", label="Node.js", ok=False, detail="missing")],
        }

        with (
            patch("core.dashboard.get_full_check_sequence", return_value=sequence),
            patch("core.dashboard.collect_check_step", side_effect=lambda step, *_: step_results[step]),
            patch("core.dashboard.get_full_check_step_label", side_effect=lambda step: f"Label:{step}"),
            patch("core.dashboard.time.monotonic", side_effect=[10.0, 11.0]),
        ):
            first_results, first_in_progress, first_text = dashboard._get_progressive_full_checks(Path("."), object())
            second_results, second_in_progress, second_text = dashboard._get_progressive_full_checks(Path("."), object())

        self.assertEqual({"python"}, set(first_results.keys()))
        self.assertIsInstance(first_results["python"], CheckSnapshot)
        self.assertTrue(first_in_progress)
        self.assertIn("1/2", first_text)
        self.assertEqual({"python", "node"}, set(second_results.keys()))
        self.assertIsInstance(second_results["node"], CheckSnapshot)
        self.assertFalse(second_in_progress)
        self.assertIn("2/2", second_text)

    def test_get_progressive_full_checks_resets_expired_cache(self) -> None:
        dashboard._RUNTIME_CACHE[dashboard._FULL_CHECK_PROGRESS_KEY] = dashboard.RuntimeCacheEntry(
            cached_at=0.0,
            payload=dashboard.FullCheckProgressState(
                results={"stale": CheckSnapshot(key="stale", label="Stale", ok=True, detail="cached")},
                next_index=1,
                updated_at=0.0,
            ),
        )

        with (
            patch("core.dashboard.get_full_check_sequence", return_value=["step-a"]),
            patch("core.dashboard.collect_check_step", return_value=[SimpleNamespace(key="fresh", label="Fresh", ok=True, detail="ok")]),
            patch("core.dashboard.get_full_check_step_label", side_effect=lambda step: f"Label:{step}"),
            patch("core.dashboard.time.monotonic", return_value=31.0),
        ):
            results, in_progress, text = dashboard._get_progressive_full_checks(Path("."), object())

        self.assertEqual({"fresh"}, set(results.keys()))
        self.assertFalse(in_progress)
        self.assertIn("已完成", text)

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
