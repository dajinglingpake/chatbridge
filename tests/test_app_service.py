from __future__ import annotations

import subprocess
import sys
import unittest
from unittest.mock import patch

from core.app_service import ServiceResult, schedule_named_action


class AppServiceTests(unittest.TestCase):
    def test_schedule_named_action_spawns_detached_runner(self) -> None:
        with patch("core.app_service.subprocess.Popen") as mocked_popen:
            result = schedule_named_action("restart", delay_seconds=1.5)
        self.assertTrue(result.ok)
        mocked_popen.assert_called_once()
        args, kwargs = mocked_popen.call_args
        command = args[0]
        self.assertEqual(sys.executable, command[0])
        self.assertEqual(["-m", "core.app_service"], command[1:3])
        self.assertIn("--run-named-action", command)
        self.assertIn("restart", command)
        self.assertIn("--delay-seconds", command)
        self.assertEqual(subprocess.DEVNULL, kwargs["stdout"])
        self.assertTrue(kwargs["start_new_session"])

    def test_schedule_named_action_rejects_unknown_action(self) -> None:
        result = schedule_named_action("restart-hub")
        self.assertEqual(ServiceResult(ok=False, message="未知操作：restart-hub"), result)
