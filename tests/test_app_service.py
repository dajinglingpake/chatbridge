from __future__ import annotations

import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

from core.app_service import ServiceResult, schedule_named_action


class AppServiceTests(unittest.TestCase):
    def test_schedule_named_action_spawns_detached_runner(self) -> None:
        proc = MagicMock()
        proc.pid = 4321
        with (
            patch("core.app_service.subprocess.Popen", return_value=proc) as mocked_popen,
            patch("core.app_service.get_runtime_snapshot") as mocked_snapshot,
            patch("core.app_service.save_json") as mocked_save_json,
            patch("core.app_service._append_action_log") as mocked_append_log,
        ):
            mocked_snapshot.return_value = MagicMock(hub_pid=101, bridge_pid=202)
            result = schedule_named_action("restart", delay_seconds=1.5)
        self.assertTrue(result.ok)
        mocked_popen.assert_called_once()
        args, kwargs = mocked_popen.call_args
        command = args[0]
        self.assertEqual(sys.executable, command[0])
        self.assertEqual(["-m", "core.app_service"], command[1:3])
        self.assertIn("--spawn-runner", command)
        self.assertIn("restart", command)
        self.assertIn("--request-id", command)
        self.assertIn("--delay-seconds", command)
        self.assertNotEqual(subprocess.DEVNULL, kwargs["stdout"])
        self.assertTrue(kwargs["start_new_session"])
        self.assertGreaterEqual(mocked_save_json.call_count, 1)
        self.assertGreaterEqual(mocked_append_log.call_count, 1)

    def test_schedule_named_action_rejects_unknown_action(self) -> None:
        result = schedule_named_action("restart-hub")
        self.assertEqual(ServiceResult(ok=False, message="未知操作：restart-hub"), result)
