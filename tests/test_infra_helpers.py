from __future__ import annotations

import tempfile
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import env_tools
import ui_main
from core.http_json import decode_json_bytes, request_json
from core.json_store import load_json


class FakeUiProcess:
    def __init__(self, pid: int, cmdline: list[str], ppid: int, cwd: str = "I:/AI/chatbridge", name: str = "pythonw.exe") -> None:
        self.pid = pid
        self.info = {"pid": pid, "ppid": ppid, "name": name, "cmdline": cmdline, "cwd": cwd}
        self._cmdline = cmdline
        self._ppid = ppid
        self._cwd = cwd
        self._name = name

    def cmdline(self) -> list[str]:
        return list(self._cmdline)

    def ppid(self) -> int:
        return self._ppid

    def name(self) -> str:
        return self._name

    def cwd(self) -> str:
        return self._cwd


class FakeUiPsutil:
    Error = Exception

    def __init__(self, processes: list[FakeUiProcess], parents: list[int]) -> None:
        self.processes = processes
        self.parents = parents

    def process_iter(self, _attrs: list[str]):
        return iter(self.processes)

    def Process(self, _pid: int):
        parents = lambda: [proc for pid in self.parents for proc in self.processes if proc.pid == pid]
        for proc in self.processes:
            if proc.pid == _pid:
                return SimpleNamespace(
                    pid=proc.pid,
                    cmdline=proc.cmdline,
                    name=proc.name,
                    ppid=proc.ppid,
                    cwd=proc.cwd,
                    parents=parents,
                )
        return SimpleNamespace(parents=parents)


class InfraHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self._window_pids_patch = patch.object(ui_main, "_window_pids_by_title", return_value=[])
        self._sleep_patch = patch.object(ui_main.time, "sleep", return_value=None)
        self._window_pids_patch.start()
        self._sleep_patch.start()

    def tearDown(self) -> None:
        self._sleep_patch.stop()
        self._window_pids_patch.stop()

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
            requirements_path.write_text("nicegui\ncryptography\nPillow\nwebsocket-client\n", encoding="utf-8")

            with patch.object(ui_main, "REQUIREMENTS_PATH", requirements_path):
                self.assertEqual(["nicegui", "cryptography", "PIL", "websocket"], ui_main._required_dependency_modules())

    def test_native_ui_startup_closes_previous_ui_processes_only(self) -> None:
        old_root = FakeUiProcess(101, ["pythonw.exe", "I:/AI/chatbridge/main.py", "--native"], 1)
        old_child = FakeUiProcess(102, ["pythonw.exe", "I:/AI/chatbridge/main.py", "--native"], 101)
        current_parent = FakeUiProcess(201, ["pythonw.exe", "I:/AI/chatbridge/main.py", "--native"], 1)
        current_child = FakeUiProcess(202, ["pythonw.exe", "I:/AI/chatbridge/main.py", "--native"], 201)
        hub_child = FakeUiProcess(301, ["pythonw.exe", "I:/AI/chatbridge/agent_hub.py"], 101)
        unrelated = FakeUiProcess(303, ["pythonw.exe", "I:/AI/chatbridge/main.py"], 1)
        fake_psutil = FakeUiPsutil([old_root, old_child, current_parent, current_child, hub_child, unrelated], parents=[201])

        with patch.object(ui_main, "psutil", fake_psutil), patch.object(ui_main.os, "getpid", return_value=202):
            with patch.object(ui_main, "_read_native_ui_pid_file", return_value=[]):
                with patch.object(ui_main, "_terminate_process_only") as terminate:
                    stopped = ui_main._close_previous_native_ui_instances(Path("I:/AI/chatbridge/main.py"))

        self.assertEqual([101, 102], stopped)
        self.assertEqual([(101,), (102,)], [call.args for call in terminate.call_args_list])

    def test_native_ui_startup_closes_previous_pywebview_window_processes(self) -> None:
        old_root = FakeUiProcess(101, ["pythonw.exe", "I:/AI/chatbridge/main.py", "--native"], 1)
        old_window = FakeUiProcess(
            401,
            [
                "pythonw.exe",
                "-c",
                "from multiprocessing.spawn import spawn_main; spawn_main(parent_pid=101, pipe_handle=2996)",
                "--multiprocessing-fork",
            ],
            101,
        )
        old_webview = FakeUiProcess(
            402,
            ["msedgewebview2.exe", "--webview-exe-name=pythonw.exe"],
            401,
            name="msedgewebview2.exe",
        )
        current_root = FakeUiProcess(202, ["pythonw.exe", "I:/AI/chatbridge/main.py", "--native"], 201)
        current_window = FakeUiProcess(
            501,
            [
                "pythonw.exe",
                "-c",
                "from multiprocessing.spawn import spawn_main; spawn_main(parent_pid=202, pipe_handle=3000)",
                "--multiprocessing-fork",
            ],
            202,
        )
        hub_child = FakeUiProcess(301, ["pythonw.exe", "I:/AI/chatbridge/agent_hub.py"], 101)
        fake_psutil = FakeUiPsutil([old_root, old_window, old_webview, current_root, current_window, hub_child], parents=[201])

        with patch.object(ui_main, "psutil", fake_psutil), patch.object(ui_main.os, "getpid", return_value=202):
            with patch.object(ui_main, "_read_native_ui_pid_file", return_value=[101]):
                with patch.object(ui_main, "_terminate_process_only") as terminate:
                    stopped = ui_main._close_previous_native_ui_instances(Path("I:/AI/chatbridge/main.py"))

        self.assertEqual({101, 401, 402}, set(stopped))
        self.assertNotIn((301,), [call.args for call in terminate.call_args_list])
        self.assertNotIn((501,), [call.args for call in terminate.call_args_list])

    def test_native_ui_startup_closes_orphan_chatbridge_window(self) -> None:
        orphan_window = FakeUiProcess(
            401,
            [
                "pythonw.exe",
                "-c",
                "from multiprocessing.spawn import spawn_main; spawn_main(parent_pid=999, pipe_handle=2996)",
                "--multiprocessing-fork",
            ],
            999,
        )
        orphan_webview = FakeUiProcess(
            402,
            ["msedgewebview2.exe", "--webview-exe-name=pythonw.exe"],
            401,
            name="msedgewebview2.exe",
        )
        current_root = FakeUiProcess(202, ["pythonw.exe", "I:/AI/chatbridge/main.py", "--native"], 201)
        fake_psutil = FakeUiPsutil([orphan_window, orphan_webview, current_root], parents=[201])

        with patch.object(ui_main, "psutil", fake_psutil), patch.object(ui_main.os, "getpid", return_value=202):
            with patch.object(ui_main, "_read_native_ui_pid_file", return_value=[]):
                with patch.object(ui_main, "_window_pids_by_title", return_value=[401]):
                    with patch.object(ui_main.time, "sleep", return_value=None):
                        with patch.object(ui_main, "_terminate_process_only") as terminate:
                            stopped = ui_main._close_previous_native_ui_instances(Path("I:/AI/chatbridge/main.py"))

        self.assertEqual({401, 402}, set(stopped))
        self.assertEqual({(401,), (402,)}, {call.args for call in terminate.call_args_list})

    def test_native_ui_startup_closes_recorded_previous_ui_pids(self) -> None:
        old_root = FakeUiProcess(101, ["pythonw.exe", "I:/AI/chatbridge/main.py", "--native"], 1)
        old_child = FakeUiProcess(102, ["pythonw.exe", "I:/AI/chatbridge/main.py", "--native"], 101)
        current_child = FakeUiProcess(202, ["pythonw.exe", "I:/AI/chatbridge/main.py", "--native"], 201)
        hub_child = FakeUiProcess(301, ["pythonw.exe", "I:/AI/chatbridge/agent_hub.py"], 101)
        fake_psutil = FakeUiPsutil([old_root, old_child, current_child, hub_child], parents=[201])

        with patch.object(ui_main, "psutil", fake_psutil), patch.object(ui_main.os, "getpid", return_value=202):
            with patch.object(ui_main, "_read_native_ui_pid_file", return_value=[101, 102, 301]):
                with patch.object(ui_main, "_terminate_process_only") as terminate:
                    stopped = ui_main._close_previous_native_ui_instances(Path("I:/AI/chatbridge/main.py"))

        self.assertEqual([101, 102], stopped)
        self.assertEqual([(101,), (102,)], [call.args for call in terminate.call_args_list])

    def test_native_ui_startup_matches_shortcut_relative_script(self) -> None:
        old_root = FakeUiProcess(101, ["pythonw.exe", "main.py", "--native"], 1)
        old_child = FakeUiProcess(102, ["pythonw.exe", "main.py", "--native"], 101)
        current_child = FakeUiProcess(202, ["pythonw.exe", "main.py", "--native"], 201)
        other_project = FakeUiProcess(301, ["pythonw.exe", "main.py", "--native"], 1, cwd="I:/AI/other")
        fake_psutil = FakeUiPsutil([old_root, old_child, current_child, other_project], parents=[201])

        with patch.object(ui_main, "psutil", fake_psutil), patch.object(ui_main.os, "getpid", return_value=202):
            with patch.object(ui_main, "_read_native_ui_pid_file", return_value=[]):
                with patch.object(ui_main, "_terminate_process_only") as terminate:
                    stopped = ui_main._close_previous_native_ui_instances(Path("I:/AI/chatbridge/main.py"))

        self.assertEqual([101, 102], stopped)
        self.assertEqual([(101,), (102,)], [call.args for call in terminate.call_args_list])

    def test_ui_startup_closes_previous_server_processes_on_same_port(self) -> None:
        old_parent = FakeUiProcess(101, ["python.exe", "main.py", "--host", "0.0.0.0", "--port", "8765"], 1, name="python.exe")
        old_child = FakeUiProcess(102, ["python.exe", "main.py", "--host", "0.0.0.0", "--port", "8765"], 101, name="python.exe")
        current_child = FakeUiProcess(202, ["python.exe", "main.py", "--host", "0.0.0.0", "--port", "8765"], 201, name="python.exe")
        other_port = FakeUiProcess(301, ["python.exe", "main.py", "--host", "0.0.0.0", "--port", "8766"], 1, name="python.exe")
        other_project = FakeUiProcess(302, ["python.exe", "main.py", "--host", "0.0.0.0", "--port", "8765"], 1, cwd="I:/AI/other", name="python.exe")
        native_ui = FakeUiProcess(303, ["pythonw.exe", "main.py", "--native", "--port", "8765"], 1)
        fake_psutil = FakeUiPsutil([old_parent, old_child, current_child, other_port, other_project, native_ui], parents=[201])

        with patch.object(ui_main, "psutil", fake_psutil), patch.object(ui_main.os, "getpid", return_value=202):
            with patch.object(ui_main, "_terminate_process_only") as terminate:
                stopped = ui_main._close_previous_ui_server_instances(8765, Path("I:/AI/chatbridge/main.py"))

        self.assertEqual([101, 102], stopped)
        self.assertEqual([(101,), (102,)], [call.args for call in terminate.call_args_list])

    def test_ui_startup_matches_default_server_port(self) -> None:
        old_default = FakeUiProcess(101, ["python.exe", "main.py"], 1, name="python.exe")
        other_project = FakeUiProcess(301, ["python.exe", "main.py"], 1, cwd="I:/AI/other", name="python.exe")
        fake_psutil = FakeUiPsutil([old_default, other_project], parents=[])

        with patch.object(ui_main, "psutil", fake_psutil), patch.object(ui_main.os, "getpid", return_value=202):
            with patch.object(ui_main, "_terminate_process_only") as terminate:
                stopped = ui_main._close_previous_ui_server_instances(8765, Path("I:/AI/chatbridge/main.py"))

        self.assertEqual([101], stopped)
        self.assertEqual([(101,)], [call.args for call in terminate.call_args_list])

    def test_ui_entry_always_closes_previous_native_instances(self) -> None:
        with (
            patch.object(ui_main, "ensure_ui_dependencies"),
            patch.object(ui_main, "_close_previous_native_ui_instances", return_value=[101]) as close_native,
            patch.object(ui_main, "_close_previous_ui_server_instances", return_value=[]) as close_server,
            patch("ui.app.run_ui") as run_ui,
        ):
            ui_main.run_ui_entry(host="127.0.0.1", port=8765, native=False, launcher_path=Path("I:/AI/chatbridge/main.py"))

        close_native.assert_called_once_with(Path("I:/AI/chatbridge/main.py"))
        close_server.assert_called_once_with(8765, Path("I:/AI/chatbridge/main.py"))
        run_ui.assert_called_once()

    def test_native_ui_pid_file_records_current_ui_family_only(self) -> None:
        current_root = FakeUiProcess(201, ["pythonw.exe", "I:/AI/chatbridge/main.py", "--native"], 1)
        current_child = FakeUiProcess(202, ["pythonw.exe", "I:/AI/chatbridge/main.py", "--native"], 201)
        shell_parent = FakeUiProcess(303, ["pwsh.exe"], 1)
        fake_psutil = FakeUiPsutil([current_root, current_child, shell_parent], parents=[201, 303])

        with patch.object(ui_main, "psutil", fake_psutil), patch.object(ui_main.os, "getpid", return_value=202):
            pids = ui_main._current_native_ui_pids(ui_main._native_ui_script_paths(Path("I:/AI/chatbridge/main.py")))

        self.assertEqual([202, 201], pids)

    def test_native_ui_termination_uses_psutil_without_taskkill_window(self) -> None:
        killed: list[int] = []

        class FakePsutil:
            Error = Exception

            @staticmethod
            def Process(pid: int):
                return SimpleNamespace(kill=lambda: killed.append(pid))

        with patch.object(ui_main, "psutil", FakePsutil):
            with patch.object(ui_main.subprocess, "run") as run:
                ui_main._terminate_process_only(123)

        self.assertEqual([123], killed)
        run.assert_not_called()

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
