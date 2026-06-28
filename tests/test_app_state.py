from __future__ import annotations

import unittest

from core.app_state import build_badge, build_issues, build_overview_lines, decide_primary_action, infer_bridge_mode
from core.state_models import CheckSnapshot, RuntimeSnapshot, BridgeRuntimeState


class AppStateTests(unittest.TestCase):
    def test_weixin_stack_running_is_full_running_without_qq(self) -> None:
        snapshot = RuntimeSnapshot(
            hub_running=True,
            hub_pid=101,
            bridge_running=True,
            bridge_pid=202,
            onebot_runtime_running=False,
            onebot_runtime_pid=None,
            qq_bridge_running=False,
            qq_bridge_pid=None,
            codex_processes=[],
            log_dir=".runtime/logs",
        )

        badge = build_badge(snapshot)
        action, _, _ = decide_primary_action(snapshot, {})

        self.assertEqual("运行中", badge.text)
        self.assertEqual("stop", action)

    def test_qq_stack_running_alone_does_not_block_weixin_start(self) -> None:
        snapshot = RuntimeSnapshot(
            hub_running=False,
            hub_pid=None,
            bridge_running=False,
            bridge_pid=None,
            onebot_runtime_running=True,
            onebot_runtime_pid=303,
            qq_bridge_running=True,
            qq_bridge_pid=404,
            codex_processes=[],
            log_dir=".runtime/logs",
        )
        checks = {
            "python": CheckSnapshot("python", "Python", True, ""),
            "project_files": CheckSnapshot("project_files", "Project files", True, ""),
            "weixin_account": CheckSnapshot("weixin_account", "Weixin account", True, ""),
        }

        badge = build_badge(snapshot)
        action, _, _ = decide_primary_action(snapshot, checks)

        self.assertEqual("已停止", badge.text)
        self.assertEqual("start", action)

    def test_full_qq_stack_running_is_running_mode(self) -> None:
        snapshot = RuntimeSnapshot(
            hub_running=True,
            hub_pid=101,
            bridge_running=False,
            bridge_pid=None,
            onebot_runtime_running=True,
            onebot_runtime_pid=303,
            qq_bridge_running=True,
            qq_bridge_pid=404,
            codex_processes=[],
            log_dir=".runtime/logs",
        )

        badge = build_badge(snapshot)
        action, _, _ = decide_primary_action(snapshot, {})

        self.assertEqual("运行中", badge.text)
        self.assertEqual("start", action)
        self.assertEqual("qq", infer_bridge_mode(snapshot))

    def test_full_qq_stack_running_has_no_process_mismatch_issue(self) -> None:
        snapshot = RuntimeSnapshot(
            hub_running=True,
            hub_pid=101,
            bridge_running=False,
            bridge_pid=None,
            onebot_runtime_running=True,
            onebot_runtime_pid=303,
            qq_bridge_running=True,
            qq_bridge_pid=404,
            codex_processes=[],
            log_dir=".runtime/logs",
        )

        issues = build_issues(snapshot, BridgeRuntimeState(started_at=""), {})

        self.assertEqual([], issues)

    def test_full_weixin_stack_infers_weixin_mode(self) -> None:
        snapshot = RuntimeSnapshot(
            hub_running=True,
            hub_pid=101,
            bridge_running=True,
            bridge_pid=202,
            onebot_runtime_running=False,
            onebot_runtime_pid=None,
            qq_bridge_running=False,
            qq_bridge_pid=None,
            codex_processes=[],
            log_dir=".runtime/logs",
        )

        self.assertEqual("weixin", infer_bridge_mode(snapshot))

    def test_build_overview_lines_renders_bridge_state_fields(self) -> None:
        snapshot = RuntimeSnapshot(
            hub_running=True,
            hub_pid=101,
            bridge_running=False,
            bridge_pid=0,
            onebot_runtime_running=False,
            onebot_runtime_pid=None,
            qq_bridge_running=False,
            qq_bridge_pid=None,
            codex_processes=[],
            log_dir=".runtime/logs",
            qq_logged_in=True,
            qq_user_id="900000001",
            qq_nickname="测试QQ",
        )
        bridge_state = BridgeRuntimeState(
            started_at="2026-01-01T00:00:00",
            last_poll_at="2026-01-01T00:01:00",
            last_message_at="2026-01-01T00:02:00",
            handled_messages=3,
            failed_messages=1,
        )

        lines = build_overview_lines(snapshot, bridge_state, "acct-1")

        self.assertIn("当前账号: acct-1", lines)
        self.assertIn("QQ 登录: 已登录 测试QQ (900000001)", lines)
        self.assertIn("微信桥状态:", lines)
        self.assertIn("started_at: 2026-01-01T00:00:00", lines)

    def test_build_issues_uses_bridge_runtime_error(self) -> None:
        snapshot = RuntimeSnapshot(
            hub_running=True,
            hub_pid=101,
            bridge_running=True,
            codex_processes=[],
            bridge_pid=202,
            onebot_runtime_running=False,
            onebot_runtime_pid=None,
            qq_bridge_running=False,
            qq_bridge_pid=None,
            log_dir=".runtime/logs",
        )
        bridge_state = BridgeRuntimeState(
            started_at="2026-01-01T00:00:00",
            last_error=" bridge failed ",
        )

        issues = build_issues(snapshot, bridge_state, {})

        self.assertEqual(1, len(issues))
        self.assertEqual("logs", issues[0].kind)
        self.assertEqual("bridge failed", issues[0].detail)


if __name__ == "__main__":
    unittest.main()
