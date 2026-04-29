from __future__ import annotations

import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from core.http_json import decode_json_bytes, request_json
from core.json_store import load_json


class InfraHelperTests(unittest.TestCase):
    def test_load_json_returns_default_for_invalid_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "broken.json"
            path.write_bytes(b"\xff\xfe\xfd")
            self.assertEqual({"ok": False}, load_json(path, {"ok": False}, expect_type=dict))

    def test_decode_json_bytes_rejects_invalid_json(self) -> None:
        with self.assertRaises(RuntimeError):
            decode_json_bytes(b"{invalid")

    def test_decode_json_bytes_rejects_non_object_payload(self) -> None:
        with self.assertRaises(RuntimeError):
            decode_json_bytes(b"[1, 2, 3]")

    def test_request_json_normalizes_socket_timeout(self) -> None:
        request = urllib.request.Request("http://example.invalid")

        with patch("urllib.request.urlopen", side_effect=TimeoutError("The read operation timed out")):
            with self.assertRaises(RuntimeError) as context:
                request_json(request, timeout=0.1)

        self.assertEqual("timed out", str(context.exception))


if __name__ == "__main__":
    unittest.main()
