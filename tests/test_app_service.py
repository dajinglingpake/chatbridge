from __future__ import annotations

import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core import app_service
from core.app_service import ServiceResult, run_named_action, schedule_named_action, submit_hub_task
from core.bridge_notifier import NoticeResult


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

    def test_direct_self_disruptive_action_delegates_to_detached_runner(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("core.app_service.spawn_named_action_runner", return_value=4321) as mocked_spawn,
            patch("core.app_service._write_action_state") as mocked_write_state,
            patch("core.app_service._append_action_log") as mocked_append_log,
        ):
            exit_code = app_service._main(
                [
                    "--run-named-action",
                    "restart-qq-stack",
                    "--request-id",
                    "svc-test",
                    "--delay-seconds",
                    "0",
                ]
            )

        self.assertEqual(0, exit_code)
        mocked_spawn.assert_called_once_with("restart-qq-stack", "svc-test", 0.0)
        mocked_write_state.assert_called_once()
        self.assertEqual("delegated", mocked_write_state.call_args.kwargs["status"])
        mocked_append_log.assert_called_once()
        self.assertEqual("delegated", mocked_append_log.call_args.args[0])

    def test_runner_child_executes_self_disruptive_action(self) -> None:
        def run_named_action(action: str) -> ServiceResult:
            self.assertNotIn(app_service.ACTION_RUNNER_ENV, __import__("os").environ)
            return ServiceResult(ok=True, message=f"done {action}")

        with (
            patch.dict("os.environ", {app_service.ACTION_RUNNER_ENV: "1"}, clear=True),
            patch("core.app_service.get_runtime_snapshot") as mocked_snapshot,
            patch("core.app_service.run_named_action", side_effect=run_named_action) as mocked_run,
            patch("core.app_service._write_action_state"),
            patch("core.app_service._append_action_log"),
        ):
            mocked_snapshot.return_value = MagicMock(hub_pid=101, bridge_pid=None)
            exit_code = app_service._main(
                [
                    "--run-named-action",
                    "restart-qq-stack",
                    "--request-id",
                    "svc-test",
                    "--delay-seconds",
                    "0",
                ]
            )

        self.assertEqual(0, exit_code)
        mocked_run.assert_called_once_with("restart-qq-stack")

    def test_restart_action_prepares_hub_tasks_before_restarting(self) -> None:
        def restart_qq_stack() -> list[str]:
            return ["Hub restarted", "QQ Bridge restarted"]

        with (
            patch("core.app_service.restart_qq_stack", side_effect=restart_qq_stack),
            patch("core.app_service.create_request", return_value="req-prepare") as mocked_create,
            patch("core.app_service.wait_for_response") as mocked_wait,
        ):
            mocked_wait.return_value = SimpleNamespace(ok=True, error="", payload={"interrupted_count": 1})
            result = run_named_action("restart-qq-stack")

        self.assertTrue(result.ok)
        mocked_create.assert_called_once_with(
            "prepare_restart",
            {"reason": "Hub restart requested by service action: restart-qq-stack"},
        )
        mocked_wait.assert_called_once_with("req-prepare", timeout_seconds=5)
        self.assertIn("Hub restart prepared 1 active task(s)", result.message)
        self.assertIn("Hub restarted", result.message)

    def test_schedule_named_action_rejects_unknown_action(self) -> None:
        result = schedule_named_action("restart-hub")
        self.assertEqual(ServiceResult(ok=False, message="未知操作：restart-hub"), result)

    def test_stop_action_sends_notice_before_stopping_services(self) -> None:
        events: list[str] = []

        def stop_all() -> list[str]:
            events.append("stop")
            return ["Bridge stopped", "Hub stopped"]

        def notify(kind: str, title: str, detail: str, **_kwargs):
            events.append("notify")
            self.assertEqual("service", kind)
            self.assertEqual("服务操作: stop", title)
            self.assertIn("即将执行服务停止操作: stop", detail)
            return NoticeResult(sent_count=1, recipient_count=1)

        with (
            patch("core.app_service.stop_all", side_effect=stop_all),
            patch("core.app_service.broadcast_bridge_notice_by_kind", side_effect=notify) as mocked_notify,
            patch("core.app_service.get_runtime_snapshot") as mocked_snapshot,
            patch("core.app_service.time.sleep") as mocked_sleep,
        ):
            mocked_snapshot.return_value = MagicMock(hub_pid=101, bridge_pid=202, bridge_running=True)
            result = run_named_action("stop")

        self.assertTrue(result.ok)
        self.assertEqual(["notify", "stop"], events)
        mocked_notify.assert_called_once()
        mocked_sleep.assert_called_once()

    def test_stop_action_in_qq_mode_does_not_notify_weixin(self) -> None:
        with (
            patch("core.app_service.stop_all", return_value=["QQ Bridge stopped", "OneBot stopped", "Hub stopped"]) as mocked_stop,
            patch("core.app_service.broadcast_bridge_notice_by_kind") as mocked_notify,
            patch("core.app_service.get_runtime_snapshot") as mocked_snapshot,
        ):
            mocked_snapshot.return_value = MagicMock(
                hub_pid=101,
                bridge_pid=None,
                onebot_runtime_pid=303,
                qq_bridge_pid=404,
                bridge_running=False,
            )
            result = run_named_action("stop")

        self.assertTrue(result.ok)
        self.assertEqual("QQ Bridge stopped | OneBot stopped | Hub stopped", result.message)
        mocked_stop.assert_called_once()
        mocked_notify.assert_not_called()

    def test_start_action_keeps_post_action_notice(self) -> None:
        events: list[str] = []

        def start_all() -> list[str]:
            events.append("start")
            return ["Hub started", "Bridge started"]

        def notify(kind: str, title: str, detail: str, **_kwargs):
            events.append("notify")
            self.assertEqual("服务操作: start", title)
            self.assertEqual("Hub started | Bridge started", detail)
            return NoticeResult(sent_count=1, recipient_count=1)

        with (
            patch("core.app_service.start_all", side_effect=start_all),
            patch("core.app_service.broadcast_bridge_notice_by_kind", side_effect=notify) as mocked_notify,
        ):
            result = run_named_action("start")

        self.assertTrue(result.ok)
        self.assertEqual(["start", "notify"], events)
        mocked_notify.assert_called_once()

    def test_start_weixin_action_uses_weixin_notice(self) -> None:
        with (
            patch("core.app_service.start_all", return_value=["QQ stopped", "Hub started", "Bridge started"]) as mocked_start,
            patch("core.app_service.broadcast_bridge_notice_by_kind", return_value=NoticeResult(sent_count=1, recipient_count=1, platform_label="微信")) as mocked_notify,
        ):
            result = run_named_action("start-weixin")

        self.assertTrue(result.ok)
        self.assertIn("已通知 1 个微信会话", result.message)
        mocked_start.assert_called_once()
        mocked_notify.assert_called_once()

    def test_qq_bridge_action_does_not_notify_weixin(self) -> None:
        with (
            patch("core.app_service.restart_qq_bridge", return_value=["QQ Bridge stopped", "QQ Bridge started"]) as mocked_restart,
            patch("core.app_service.broadcast_bridge_notice_by_kind") as mocked_notify,
        ):
            result = run_named_action("restart-qq-bridge")

        self.assertTrue(result.ok)
        self.assertEqual("QQ Bridge stopped | QQ Bridge started", result.message)
        mocked_restart.assert_called_once()
        mocked_notify.assert_not_called()

    def test_restart_onebot_runtime_is_allowed_without_weixin_notice(self) -> None:
        with (
            patch("core.app_service.restart_onebot_runtime", return_value=["OneBot restarted"]) as mocked_restart,
            patch("core.app_service.broadcast_bridge_notice_by_kind") as mocked_notify,
        ):
            result = run_named_action("restart-onebot-runtime")

        self.assertTrue(result.ok)
        self.assertEqual("OneBot restarted", result.message)
        mocked_restart.assert_called_once()
        mocked_notify.assert_not_called()

    def test_submit_qq_web_task_does_not_notify_weixin(self) -> None:
        response = SimpleNamespace(ok=True, payload={"task": {"id": "task-qq-web-001"}}, error="")
        with (
            patch("core.app_service.create_request", return_value="req-qq-web") as mocked_create,
            patch("core.app_service.wait_for_response", return_value=response),
            patch("core.app_service.broadcast_bridge_notice_by_kind") as mocked_notify,
        ):
            result = submit_hub_task("main", "hello", source="qq-web")

        self.assertTrue(result.ok)
        self.assertEqual("任务已入队：task-qq-web-001", result.message)
        payload = mocked_create.call_args.args[1]
        self.assertEqual("qq-web", payload["source"])
        mocked_notify.assert_not_called()
