from __future__ import annotations

import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

from core.app_service import ServiceResult, run_named_action, schedule_named_action
from core.weixin_notifier import NoticeResult


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

    def test_stop_action_sends_notice_before_stopping_services(self) -> None:
        events: list[str] = []

        def stop_all() -> list[str]:
            events.append("stop")
            return ["Bridge stopped", "Hub stopped"]

        def notify(kind: str, title: str, detail: str):
            events.append("notify")
            self.assertEqual("service", kind)
            self.assertEqual("服务操作: stop", title)
            self.assertIn("即将执行服务停止操作: stop", detail)
            return NoticeResult(sent_count=1, recipient_count=1)

        with (
            patch("core.app_service.stop_all", side_effect=stop_all),
            patch("core.app_service.broadcast_weixin_notice_by_kind", side_effect=notify) as mocked_notify,
            patch("core.app_service.get_runtime_snapshot") as mocked_snapshot,
            patch("core.app_service.time.sleep") as mocked_sleep,
        ):
            mocked_snapshot.return_value = MagicMock(hub_pid=101, bridge_pid=202)
            result = run_named_action("stop")

        self.assertTrue(result.ok)
        self.assertEqual(["notify", "stop"], events)
        mocked_notify.assert_called_once()
        mocked_sleep.assert_called_once()

    def test_start_action_keeps_post_action_notice(self) -> None:
        events: list[str] = []

        def start_all() -> list[str]:
            events.append("start")
            return ["Hub started", "Bridge started"]

        def notify(kind: str, title: str, detail: str):
            events.append("notify")
            self.assertEqual("服务操作: start", title)
            self.assertEqual("Hub started | Bridge started", detail)
            return NoticeResult(sent_count=1, recipient_count=1)

        with (
            patch("core.app_service.start_all", side_effect=start_all),
            patch("core.app_service.broadcast_weixin_notice_by_kind", side_effect=notify) as mocked_notify,
        ):
            result = run_named_action("start")

        self.assertTrue(result.ok)
        self.assertEqual(["start", "notify"], events)
        mocked_notify.assert_called_once()
