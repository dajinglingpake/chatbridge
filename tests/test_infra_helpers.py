from __future__ import annotations

import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

import env_tools
import ui_main
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

    def test_ui_dependency_modules_are_loaded_from_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            requirements_path = Path(temp_dir) / "requirements.txt"
            requirements_path.write_text("nicegui\ncryptography\nPillow\n", encoding="utf-8")

            with patch.object(ui_main, "REQUIREMENTS_PATH", requirements_path):
                self.assertEqual(["nicegui", "cryptography", "PIL"], ui_main._required_dependency_modules())

    def test_python_dependency_check_reports_missing_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            requirements_path = Path(temp_dir) / "requirements.txt"
            requirements_path.write_text("definitely-missing-chatbridge-package\n", encoding="utf-8")

            with patch.object(env_tools, "REQUIREMENTS_PATH", requirements_path):
                result = env_tools._python_dependencies_check()

        self.assertFalse(result.ok)
        self.assertEqual("psutil", result.key)
        self.assertIn("definitely_missing_chatbridge_package", result.detail)


if __name__ == "__main__":
    unittest.main()
