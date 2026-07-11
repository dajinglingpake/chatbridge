from __future__ import annotations

import unittest

from ui.app import _ClientState


class ClientStateTests(unittest.TestCase):
    def test_state_isolated_between_client_storages(self) -> None:
        storages: list[dict[str, object]] = [{}, {}]
        active_client = 0
        state = _ClientState(
            {
                "selected_session_name": "",
                "stream_session_task_limits": {},
            },
            lambda: storages[active_client],
        )

        state["selected_session_name"] = "qq-private-1753473884"
        limits = state["stream_session_task_limits"]
        self.assertIsInstance(limits, dict)
        limits["qq-private-1753473884"] = 40

        active_client = 1
        self.assertEqual("", state["selected_session_name"])
        self.assertEqual({}, state["stream_session_task_limits"])

        state["selected_session_name"] = "codex:thread-2"
        active_client = 0
        self.assertEqual("qq-private-1753473884", state["selected_session_name"])
        self.assertEqual({"qq-private-1753473884": 40}, state["stream_session_task_limits"])
