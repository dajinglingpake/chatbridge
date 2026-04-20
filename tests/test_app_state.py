from __future__ import annotations

import unittest

from core.app_state import build_issues, build_overview_lines
from core.state_models import RuntimeSnapshot, WeixinBridgeRuntimeState


class AppStateTests(unittest.TestCase):
    def test_build_overview_lines_renders_bridge_state_fields(self) -> None:
        snapshot = RuntimeSnapshot(
            hub_running=True,
            hub_pid=101,
            bridge_running=False,
            bridge_pid=0,
            codex_processes=[],
            log_dir=".runtime/logs",
        )
        bridge_state = WeixinBridgeRuntimeState(
            started_at="2026-01-01T00:00:00",
            last_poll_at="2026-01-01T00:01:00",
            last_message_at="2026-01-01T00:02:00",
            handled_messages=3,
            failed_messages=1,
        )

        lines = build_overview_lines(snapshot, bridge_state, "acct-1")

        self.assertIn("当前账号: acct-1", lines)
        self.assertIn("微信桥状态:", lines)
        self.assertIn("started_at: 2026-01-01T00:00:00", lines)

    def test_build_issues_uses_bridge_runtime_error(self) -> None:
        snapshot = RuntimeSnapshot(
            hub_running=True,
            hub_pid=101,
            bridge_running=True,
            codex_processes=[],
            bridge_pid=202,
            log_dir=".runtime/logs",
        )
        bridge_state = WeixinBridgeRuntimeState(
            started_at="2026-01-01T00:00:00",
            last_error=" bridge failed ",
        )

        issues = build_issues(snapshot, bridge_state, {})

        self.assertEqual(1, len(issues))
        self.assertEqual("logs", issues[0].kind)
        self.assertEqual("bridge failed", issues[0].detail)


if __name__ == "__main__":
    unittest.main()
