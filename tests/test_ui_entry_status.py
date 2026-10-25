from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import patch

from ui.app import APP_DIR, _load_codex_runtime_statuses, _refresh_runtime_status_on_ui_entry


class UIEntryStatusTests(unittest.TestCase):
    def test_ui_entry_forces_one_runtime_status_refresh(self) -> None:
        with patch("ui.app.refresh_dashboard_cache") as refresh_cache:
            _refresh_runtime_status_on_ui_entry()

        refresh_cache.assert_called_once_with(APP_DIR, "runtime")


class RuntimeStatusLoadingTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_hub_does_not_block_ui_event_loop(self) -> None:
        started = threading.Event()
        release = threading.Event()
        statuses = {"thread-1": {"type": "running"}}

        def slow_query(*, timeout_seconds: float):
            started.set()
            if not release.wait(timeout=2):
                raise TimeoutError("UI event loop did not release the query")
            return statuses

        with patch("ui.app.list_codex_thread_statuses", side_effect=slow_query):
            task = asyncio.create_task(_load_codex_runtime_statuses())
            try:
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(started.is_set())
                self.assertFalse(task.done())
            finally:
                release.set()
                result = await task

        self.assertEqual(statuses, result)

    async def test_unavailable_hub_returns_empty_statuses(self) -> None:
        with patch("ui.app.list_codex_thread_statuses", side_effect=TimeoutError):
            self.assertEqual({}, await _load_codex_runtime_statuses())


if __name__ == "__main__":
    unittest.main()
