from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.http_json import decode_json_bytes
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


if __name__ == "__main__":
    unittest.main()
