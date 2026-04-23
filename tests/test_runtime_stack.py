from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from runtime_stack import _taskkill, start_managed, stop_managed


class RuntimeStackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.root = Path(self._tempdir.name)

    def test_start_managed_cleans_duplicate_processes_and_keeps_primary(self) -> None:
        primary = SimpleNamespace(pid=101)
        duplicate = SimpleNamespace(pid=202)
        pid_file = self.root / "agent.pid"
        with patch("runtime_stack._find_processes_by_script", return_value=[primary, duplicate]):
            with patch("runtime_stack._taskkill") as mocked_kill:
                with patch("runtime_stack._write_pid_file") as mocked_write_pid:
                    message = start_managed("Hub", self.root / "agent_hub.py", pid_file, self.root / "out.log", self.root / "err.log")
        mocked_kill.assert_called_once_with(202)
        mocked_write_pid.assert_called_once_with(pid_file, 101)
        self.assertIn("cleaned duplicate PIDs 202", message)

    def test_stop_managed_stops_all_duplicate_processes(self) -> None:
        first = SimpleNamespace(pid=101)
        second = SimpleNamespace(pid=202)
        pid_file = self.root / "bridge.pid"
        with patch("runtime_stack._find_processes_by_script", return_value=[first, second]):
            with patch("runtime_stack._taskkill") as mocked_kill:
                with patch("runtime_stack._clear_pid_file") as mocked_clear:
                    message = stop_managed("Bridge", self.root / "weixin_hub_bridge.py", pid_file)
        self.assertEqual([(101,), (202,)], [call.args for call in mocked_kill.call_args_list])
        mocked_clear.assert_called_once_with(pid_file)
        self.assertIn("PIDs 101, 202", message)

    def test_taskkill_skips_current_process_when_stopping_children(self) -> None:
        current_pid = os.getpid()

        class FakeChild:
            def __init__(self, pid: int) -> None:
                self.pid = pid
                self.terminated = False

            def terminate(self) -> None:
                self.terminated = True

            def wait(self, timeout: float | None = None) -> None:
                return None

            def kill(self) -> None:
                return None

        class FakeProc:
            def __init__(self, children: list[FakeChild]) -> None:
                self._children = children
                self.terminated = False

            def children(self, recursive: bool = False) -> list[FakeChild]:
                return list(self._children)

            def terminate(self) -> None:
                self.terminated = True

            def wait(self, timeout: float | None = None) -> None:
                return None

            def kill(self) -> None:
                return None

        current_child = FakeChild(current_pid)
        other_child = FakeChild(999999)
        proc = FakeProc([current_child, other_child])
        with patch("runtime_stack.psutil", object()):
            with patch("runtime_stack._get_process", return_value=proc):
                _taskkill(123)
        self.assertFalse(current_child.terminated)
        self.assertTrue(other_child.terminated)
        self.assertTrue(proc.terminated)

    def test_taskkill_ignores_oserror_from_psutil_wait(self) -> None:
        class FakeChild:
            pid = 999999

            def __init__(self) -> None:
                self.terminated = False
                self.killed = False

            def terminate(self) -> None:
                self.terminated = True

            def wait(self, timeout: float | None = None) -> None:
                raise OSError(22, "Invalid argument")

            def kill(self) -> None:
                self.killed = True

        class FakeProc:
            pid = 123

            def __init__(self, child: FakeChild) -> None:
                self.child = child
                self.terminated = False
                self.killed = False

            def children(self, recursive: bool = False) -> list[FakeChild]:
                return [self.child]

            def terminate(self) -> None:
                self.terminated = True

            def wait(self, timeout: float | None = None) -> None:
                raise OSError(22, "Invalid argument")

            def kill(self) -> None:
                self.killed = True

        child = FakeChild()
        proc = FakeProc(child)
        fake_psutil = SimpleNamespace(Error=Exception)
        with patch("runtime_stack.psutil", fake_psutil):
            with patch("runtime_stack._get_process", return_value=proc):
                _taskkill(123)
        self.assertTrue(proc.terminated)
        self.assertTrue(proc.killed)
        self.assertTrue(child.terminated)
        self.assertTrue(child.killed)
