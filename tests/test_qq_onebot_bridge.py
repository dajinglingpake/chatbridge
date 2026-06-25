from __future__ import annotations

import tempfile
import threading
import unittest
import socket
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from qq_onebot_bridge import DEFAULT_QQ_AGENT_ID, QQOneBotBridge
from http.server import ThreadingHTTPServer


NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class FakeQQBridge(QQOneBotBridge):
    def __init__(self, temp_path: Path) -> None:
        self.temp_path = temp_path
        self.submitted: list[tuple[str, str]] = []
        self.replies: list[tuple[dict[str, object], str]] = []
        self.api_calls: list[tuple[str, dict[str, object]]] = []
        super().__init__(
            SimpleNamespace(backend_id="main", default_backend="codex", hub_task_timeout_seconds=30),
            api_base="http://onebot.local",
        )

    def _download_media_url(self, sender_key: str, url: str, *, filename: str) -> Path:
        target = self.temp_path / f"{sender_key.replace(':', '-')}-{filename}"
        target.write_bytes(f"{url}:{filename}".encode("utf-8"))
        return target

    def _submit_task(self, sender_key: str, prompt: str) -> dict[str, object]:
        self.submitted.append((sender_key, prompt))
        return {"id": "task-qq-001"}

    def _wait_and_reply(self, event: dict[str, object], task_id: str) -> None:
        self.replies.append((event, task_id))

    def _onebot_api(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        self.api_calls.append((action, payload))
        return {"status": "ok"}


class QQOneBotBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.temp_path = Path(self._tempdir.name)
        self.state_path = self.temp_path / "qq_media.json"
        patcher = patch("qq_onebot_bridge.ONEBOT_STATE_PATH", self.state_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_media_only_message_caches_attachment_for_next_text(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message": [
                    {"type": "image", "data": {"url": "http://media.local/image", "file": "image.png"}},
                ],
            }
        )

        self.assertEqual([], bridge.submitted)
        self.assertIn("qq:private:10001", bridge.pending_media_context)

        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": 10001,
                "message": [{"type": "text", "data": {"text": "看下这张图"}}],
            }
        )

        self.assertEqual(1, len(bridge.submitted))
        sender_key, prompt = bridge.submitted[0]
        self.assertEqual("qq:private:10001", sender_key)
        self.assertTrue(prompt.startswith("看下这张图"))
        self.assertIn("图片: image.png", prompt)
        self.assertIn("qq-private-10001-image.png", prompt)
        self.assertNotIn("qq:private:10001", bridge.pending_media_context)

    def test_group_text_with_file_submits_prompt_with_attachment(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 20002,
                "user_id": 10001,
                "self_id": 2493227263,
                "message": [
                    {"type": "at", "data": {"qq": "2493227263"}},
                    {"type": "text", "data": {"text": "总结这个文件"}},
                    {"type": "file", "data": {"url": "http://media.local/file", "file": "report.pdf"}},
                ],
            }
        )

        sender_key, prompt = bridge.submitted[0]
        self.assertEqual("qq:group:20002:10001", sender_key)
        self.assertTrue(prompt.startswith("总结这个文件"))
        self.assertIn("文件: report.pdf", prompt)

    def test_group_message_without_at_self_is_ignored(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 20002,
                "user_id": 10001,
                "self_id": 2493227263,
                "message": [
                    {"type": "text", "data": {"text": "不要回复这条"}},
                ],
            }
        )

        self.assertEqual([], bridge.submitted)

    def test_group_message_at_other_user_is_ignored(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge.handle_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 20002,
                "user_id": 10001,
                "self_id": 2493227263,
                "message": [
                    {"type": "at", "data": {"qq": "10002"}},
                    {"type": "text", "data": {"text": "也不要回复这条"}},
                ],
            }
        )

        self.assertEqual([], bridge.submitted)

    def test_send_reply_uses_private_or_group_api(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        bridge._send_reply({"message_type": "private", "user_id": 10001}, "hello")
        bridge._send_reply({"message_type": "group", "group_id": 20002}, "world")

        self.assertEqual(
            [
                ("send_private_msg", {"user_id": 10001, "message": "hello"}),
                ("send_group_msg", {"group_id": 20002, "message": "world"}),
            ],
            bridge.api_calls,
        )

    def test_submit_task_uses_dedicated_qq_agent_and_sender_session(self) -> None:
        bridge = QQOneBotBridge(
            SimpleNamespace(backend_id="main", default_backend="codex", hub_task_timeout_seconds=30),
            api_base="http://onebot.local",
        )
        calls: list[tuple[str, dict[str, object]]] = []

        def fake_create_request(action: str, payload: dict[str, object]) -> str:
            calls.append((action, payload))
            return "req-qq-001"

        response = SimpleNamespace(ok=True, error="", payload={"task": {"id": "task-qq-001"}})
        with patch("qq_onebot_bridge.create_request", side_effect=fake_create_request), patch("qq_onebot_bridge.wait_for_response", return_value=response):
            task = bridge._submit_task("qq:private:10001", "hello")

        self.assertEqual({"id": "task-qq-001"}, task)
        self.assertEqual("submit_task", calls[0][0])
        self.assertEqual(DEFAULT_QQ_AGENT_ID, calls[0][1]["agent_id"])
        self.assertEqual("qq-private-10001", calls[0][1]["session_name"])
        self.assertEqual("qq", calls[0][1]["source"])

    def test_http_handler_returns_onebot_ok_for_message_event(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        server = ThreadingHTTPServer(("127.0.0.1", 0), bridge.make_handler())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/",
            data=b'{"post_type":"message","message_type":"private","user_id":10001,"message":"hello"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with NO_PROXY_OPENER.open(request, timeout=5) as response:
            body = response.read().decode("utf-8")

        self.assertEqual(200, response.status)
        self.assertIn('"retcode":0', body)

    def test_http_handler_does_not_return_400_for_invalid_payload(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        server = ThreadingHTTPServer(("127.0.0.1", 0), bridge.make_handler())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/",
            data=b"not-json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with NO_PROXY_OPENER.open(request, timeout=5) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            self.fail(f"handler returned unexpected HTTP error: {exc.code}")

        self.assertEqual(200, response.status)
        self.assertIn('"retcode":0', body)

    def test_http_handler_reads_chunked_onebot_event(self) -> None:
        bridge = FakeQQBridge(self.temp_path)
        server = ThreadingHTTPServer(("127.0.0.1", 0), bridge.make_handler())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        body = b'{"post_type":"message","message_type":"private","user_id":10001,"message":"chunked hello"}'
        first = body[:23]
        second = body[23:]
        raw_request = (
            f"POST / HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\nTransfer-Encoding: chunked\r\nContent-Type: application/json\r\n\r\n".encode(
                "ascii"
            )
            + f"{len(first):X}\r\n".encode("ascii")
            + first
            + b"\r\n"
            + f"{len(second):X}\r\n".encode("ascii")
            + second
            + b"\r\n0\r\n\r\n"
        )
        with socket.create_connection(("127.0.0.1", server.server_port), timeout=5) as sock:
            sock.sendall(raw_request)
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                if b'{"status":"ok","retcode":0,"data":null}' in b"".join(chunks):
                    break
            response = b"".join(chunks).decode("utf-8", errors="replace")

        self.assertIn("200 OK", response)
        self.assertIn('"retcode":0', response)
        self.assertEqual(1, len(bridge.submitted))
        self.assertEqual("chunked hello", bridge.submitted[0][1])


if __name__ == "__main__":
    unittest.main()
