from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import local_ipc


class LocalIpcBridgeChannelTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        root = Path(self._tempdir.name) / "ipc"
        patchers = [
            patch("local_ipc.IPC_DIR", root),
            patch("local_ipc.REQUEST_DIR", root / "requests"),
            patch("local_ipc.RESPONSE_DIR", root / "responses"),
            patch("local_ipc.PROCESSED_DIR", root / "processed"),
            patch("local_ipc.BRIDGE_REQUEST_DIR", root / "bridge_requests"),
            patch("local_ipc.BRIDGE_PROCESSED_DIR", root / "bridge_processed"),
            patch("local_ipc.BRIDGE_CHANNELS_DIR", root / "bridge_channels"),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_bridge_request_channel_is_isolated_from_default_wechat_queue(self) -> None:
        request_id = local_ipc.create_bridge_request("task_update", {"task": {"id": "task-qq-001"}}, channel="qq")

        qq_request_path = local_ipc.bridge_request_dir("qq") / f"{request_id}.json"
        self.assertTrue(qq_request_path.exists())
        self.assertFalse((local_ipc.bridge_request_dir("wechat") / f"{request_id}.json").exists())

        request = local_ipc.read_request(qq_request_path)
        self.assertEqual("task_update", request.action)
        local_ipc.mark_bridge_processed(qq_request_path, channel="qq")

        self.assertFalse(qq_request_path.exists())
        self.assertTrue((local_ipc.bridge_processed_dir("qq") / f"{request_id}.json").exists())


if __name__ == "__main__":
    unittest.main()
