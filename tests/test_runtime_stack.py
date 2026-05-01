from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from runtime_stack import _managed_subprocess_env, _taskkill, discover_external_agent_processes, start_managed, stop_managed


class FakeProcess:
    def __init__(self, pid: int, name: str, cmdline: list[str] | None = None, ppid: int | None = None) -> None:
        self.info = {"pid": pid, "name": name}
        self.pid = pid
        self._cmdline = cmdline or []
        self._ppid = ppid
        self.cmdline_accessed = False

    def cmdline(self) -> list[str]:
        self.cmdline_accessed = True
        return list(self._cmdline)

    def ppid(self) -> int | None:
        return self._ppid


class FakePsutil:
    Error = Exception

    def __init__(self, processes: list[FakeProcess]) -> None:
        self.processes = processes
        self.attrs: list[list[str]] = []

    def process_iter(self, attrs: list[str]):
        self.attrs.append(list(attrs))
        return iter(self.processes)


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

    def test_start_managed_passes_proxy_env_to_child_process(self) -> None:
        pid_file = self.root / "agent.pid"
        with patch.dict("runtime_stack.os.environ", {"HTTPS_PROXY": "http://127.0.0.1:7890"}, clear=True):
            with patch("runtime_stack._find_processes_by_script", return_value=[]):
                with patch("runtime_stack._get_python_command", return_value="/usr/bin/python3"):
                    with patch("runtime_stack.subprocess.Popen", return_value=SimpleNamespace(pid=123)) as mocked_popen:
                        start_managed("Hub", self.root / "agent_hub.py", pid_file, self.root / "out.log", self.root / "err.log")
        env = mocked_popen.call_args.kwargs["env"]
        self.assertEqual("http://127.0.0.1:7890", env["HTTPS_PROXY"])

    def test_managed_subprocess_env_copies_proxy_from_running_process(self) -> None:
        fake_proc = SimpleNamespace(pid=123)
        with patch.dict("runtime_stack.os.environ", {}, clear=True):
            with patch("runtime_stack._find_processes_by_script", return_value=[fake_proc]):
                with patch("runtime_stack._read_process_proxy_env", return_value={"HTTPS_PROXY": "http://127.0.0.1:7890"}):
                    env = _managed_subprocess_env({})
        self.assertEqual("http://127.0.0.1:7890", env["HTTPS_PROXY"])

    def test_discover_external_agents_skips_cmdline_for_unrelated_processes(self) -> None:
        unrelated = FakeProcess(101, "chrome.exe", ["chrome.exe", "--type=renderer"])
        codex = FakeProcess(202, "Codex.exe", ["Codex.exe", "resume", "session-123"])
        fake_psutil = FakePsutil([unrelated, codex])

        with patch("runtime_stack.psutil", fake_psutil):
            with patch("runtime_stack.os.getpid", return_value=999):
                with patch("runtime_stack._managed_root_pids", return_value=set()):
                    with patch("runtime_stack._has_managed_ancestor", return_value=False):
                        discovered = discover_external_agent_processes()

        self.assertEqual([["pid", "name"]], fake_psutil.attrs)
        self.assertFalse(unrelated.cmdline_accessed)
        self.assertTrue(codex.cmdline_accessed)
        self.assertEqual([202], [item.pid for item in discovered])
        self.assertEqual("session-123", discovered[0].session_hint)

    def test_discover_external_agents_reads_host_process_cmdline_when_needed(self) -> None:
        node = FakeProcess(303, "node.exe", ["node.exe", "C:/tools/codex/index.js", "resume", "session-456"])
        fake_psutil = FakePsutil([node])

        with patch("runtime_stack.psutil", fake_psutil):
            with patch("runtime_stack.os.getpid", return_value=999):
                with patch("runtime_stack._managed_root_pids", return_value=set()):
                    with patch("runtime_stack._has_managed_ancestor", return_value=False):
                        discovered = discover_external_agent_processes()

        self.assertTrue(node.cmdline_accessed)
        self.assertEqual([303], [item.pid for item in discovered])
        self.assertEqual("session-456", discovered[0].session_hint)

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
