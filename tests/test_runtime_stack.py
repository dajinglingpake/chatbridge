from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from runtime_stack import (
    _filter_child_process_matches,
    _latest_qq_login_audit,
    get_qq_login_status,
    get_managed_status,
    get_runtime_snapshot,
    _managed_subprocess_env,
    _onebot_runtime_env,
    _taskkill,
    discover_external_agent_processes,
    restart_all,
    restart_hub,
    restart_qq_bridge,
    restart_qq_stack,
    start_all,
    start_managed,
    start_qq_stack,
    stop_managed,
    stop_onebot_runtime,
)


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

    def test_start_managed_restarts_cleanly_when_duplicate_processes_exist(self) -> None:
        primary = SimpleNamespace(pid=101)
        duplicate = SimpleNamespace(pid=202)
        restarted = SimpleNamespace(pid=303)
        pid_file = self.root / "agent.pid"
        with patch("runtime_stack._find_processes_by_script", return_value=[primary, duplicate]):
            with patch("runtime_stack._taskkill") as mocked_kill:
                with patch("runtime_stack._wait_for_script_processes_to_exit", return_value=[]):
                    with patch("runtime_stack.subprocess.Popen", return_value=restarted):
                        with patch("runtime_stack._write_pid_file") as mocked_write_pid:
                            message = start_managed("Hub", self.root / "agent_hub.py", pid_file, self.root / "out.log", self.root / "err.log")

        self.assertEqual([(101,), (202,)], [call.args for call in mocked_kill.call_args_list])
        mocked_write_pid.assert_called_once_with(pid_file, 303)
        self.assertIn("restarted after cleaning duplicate PIDs 101, 202", message)

    def test_filter_child_process_matches_keeps_only_roots(self) -> None:
        parent = FakeProcess(101, "python.exe", ppid=1)
        child = FakeProcess(202, "python.exe", ppid=101)

        filtered = _filter_child_process_matches([parent, child])

        self.assertEqual([101], [proc.pid for proc in filtered])

    def test_get_managed_status_can_skip_missing_process_discovery(self) -> None:
        pid_file = self.root / "bridge.pid"

        with (
            patch("runtime_stack._read_pid_file", return_value=None),
            patch("runtime_stack._find_process_by_script", side_effect=AssertionError("process scan should be skipped")),
            patch("runtime_stack._clear_pid_file") as mocked_clear,
        ):
            status = get_managed_status("Bridge", self.root / "weixin_hub_bridge.py", pid_file, discover=False)

        self.assertFalse(status.running)
        mocked_clear.assert_not_called()

    def test_runtime_snapshot_can_skip_qq_login_probe(self) -> None:
        managed = SimpleNamespace(running=False, pid=None)
        onebot = SimpleNamespace(running=True, pid=303)

        with (
            patch("runtime_stack.get_managed_status", return_value=managed) as mocked_status,
            patch("runtime_stack.get_onebot_runtime_status", return_value=onebot) as mocked_onebot,
            patch("runtime_stack.get_qq_login_status", side_effect=AssertionError("QQ login probe should be skipped")),
        ):
            snapshot = get_runtime_snapshot(
                include_agent_processes=False,
                include_qq_login_status=False,
                discover_missing_processes=False,
            )

        self.assertFalse(snapshot.qq_logged_in)
        self.assertEqual("", snapshot.qq_user_id)
        self.assertEqual([False, False, False], [call.kwargs["discover"] for call in mocked_status.call_args_list])
        mocked_onebot.assert_called_once_with(discover=False)

    def test_qq_login_status_prefers_napcat_webui_login_state(self) -> None:
        with (
            patch("runtime_stack.fetch_napcat_login_status", return_value={"isLogin": True, "isOffline": False}),
            patch("runtime_stack.fetch_napcat_login_info", return_value={"uin": "2493227263", "nick": "test"}),
            patch("runtime_stack._audit_qq_login_status"),
            patch("runtime_stack._query_onebot_api", side_effect=AssertionError("OneBot fallback should be skipped")),
        ):
            logged_in, user_id, nickname = get_qq_login_status()

        self.assertTrue(logged_in)
        self.assertEqual("2493227263", user_id)
        self.assertEqual("test", nickname)

    def test_qq_login_status_falls_back_to_onebot_when_napcat_reports_offline(self) -> None:
        with (
            patch("runtime_stack.fetch_napcat_login_status", return_value={"isLogin": False, "isOffline": True}),
            patch(
                "runtime_stack._query_onebot_api",
                side_effect=[
                    {"data": {"online": True, "good": True}},
                    {"data": {"user_id": 2493227263, "nickname": "test"}},
                ],
            ),
            patch("runtime_stack._audit_qq_login_status"),
        ):
            logged_in, user_id, nickname = get_qq_login_status()

        self.assertTrue(logged_in)
        self.assertEqual("2493227263", user_id)
        self.assertEqual("test", nickname)

    def test_qq_login_status_audits_status_changes(self) -> None:
        audit_log = self.root / "qq_login_status.jsonl"
        with (
            patch("runtime_stack.QQ_LOGIN_STATUS_LOG", audit_log),
            patch("runtime_stack._QQ_LOGIN_STATUS_AUDIT_LAST_SIGNATURE", None),
            patch("runtime_stack.fetch_napcat_login_status", return_value={"isLogin": True, "isOffline": False}),
            patch("runtime_stack.fetch_napcat_login_info", return_value={"uin": "2493227263", "nick": "test"}),
        ):
            get_qq_login_status()
            get_qq_login_status()

        lines = audit_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, len(lines))
        record = json.loads(lines[0])
        self.assertTrue(record["logged_in"])
        self.assertEqual("napcat", record["source"])
        self.assertEqual("2493227263", record["user_id"])
        self.assertEqual({"isLogin": True, "isOffline": False}, record["napcat_status"])

    def test_qq_login_status_audits_offline_error(self) -> None:
        audit_log = self.root / "qq_login_status.jsonl"
        with (
            patch("runtime_stack.QQ_LOGIN_STATUS_LOG", audit_log),
            patch("runtime_stack._QQ_LOGIN_STATUS_AUDIT_LAST_SIGNATURE", None),
            patch("runtime_stack.fetch_napcat_login_status", return_value={"isLogin": False, "isOffline": True, "loginError": "expired"}),
            patch("runtime_stack._query_onebot_api", side_effect=OSError("offline")),
        ):
            logged_in, user_id, nickname = get_qq_login_status()

        self.assertFalse(logged_in)
        self.assertEqual("", user_id)
        self.assertEqual("", nickname)
        record = json.loads(audit_log.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual("error", record["source"])
        self.assertFalse(record["logged_in"])
        self.assertEqual({"isLogin": False, "isOffline": True, "loginError": "expired"}, record["napcat_status"])
        self.assertEqual("onebot:OSError", record["error"])

    def test_latest_qq_login_audit_describes_login_error(self) -> None:
        audit_log = self.root / "qq_login_status.jsonl"
        audit_log.write_text(
            "\n".join(
                [
                    "{broken",
                    json.dumps(
                        {
                            "at": "2026-07-10T13:02:00+0800",
                            "logged_in": False,
                            "napcat_status": {"isLogin": False, "isOffline": True, "loginError": "你的用户身份已失效"},
                            "onebot_status": {},
                        },
                        ensure_ascii=False,
                    ),
                ]
            ),
            encoding="utf-8",
        )
        with patch("runtime_stack.QQ_LOGIN_STATUS_LOG", audit_log):
            detail, checked_at = _latest_qq_login_audit()

        self.assertEqual("NapCat 登录异常：你的用户身份已失效", detail)
        self.assertEqual("2026-07-10T13:02:00+0800", checked_at)

    def test_start_managed_passes_proxy_env_to_child_process(self) -> None:
        pid_file = self.root / "agent.pid"
        with patch.dict("runtime_stack.os.environ", {"HTTPS_PROXY": "http://127.0.0.1:7890"}, clear=True):
            with patch("runtime_stack._find_processes_by_script", return_value=[]):
                with patch("runtime_stack._get_python_command", return_value="/usr/bin/python3"):
                    with patch("runtime_stack.subprocess.Popen", return_value=SimpleNamespace(pid=123)) as mocked_popen:
                        start_managed("Hub", self.root / "agent_hub.py", pid_file, self.root / "out.log", self.root / "err.log")
        env = mocked_popen.call_args.kwargs["env"]
        self.assertEqual("http://127.0.0.1:7890", env["HTTPS_PROXY"])

    def test_start_all_starts_only_weixin_stack(self) -> None:
        with (
            patch("runtime_stack.stop_qq_bridge", return_value="QQ Bridge stopped") as mocked_stop_qq_bridge,
            patch("runtime_stack.stop_onebot_runtime", return_value="OneBot stopped") as mocked_stop_onebot,
            patch("runtime_stack.start_managed", side_effect=["Hub started", "Bridge started"]) as mocked_start,
            patch("runtime_stack.start_onebot_runtime") as mocked_start_onebot,
            patch("runtime_stack.time.sleep"),
        ):
            messages = start_all()

        self.assertEqual(["QQ Bridge stopped", "OneBot stopped", "Hub started", "Bridge started"], messages)
        self.assertEqual(["Hub", "Bridge"], [call.args[0] for call in mocked_start.call_args_list])
        mocked_start_onebot.assert_not_called()
        mocked_stop_qq_bridge.assert_called_once_with()
        mocked_stop_onebot.assert_called_once_with()

    def test_start_qq_stack_stops_weixin_bridge_and_starts_qq_stack(self) -> None:
        with (
            patch("runtime_stack.stop_bridge", return_value="Bridge stopped") as mocked_stop_bridge,
            patch("runtime_stack.start_managed", return_value="Hub started") as mocked_start,
            patch("runtime_stack.start_onebot_runtime", return_value="OneBot started") as mocked_start_onebot,
            patch("runtime_stack.start_qq_bridge", return_value="QQ Bridge started") as mocked_start_qq_bridge,
        ):
            messages = start_qq_stack()

        self.assertEqual(["Bridge stopped", "Hub started", "OneBot started", "QQ Bridge started"], messages)
        mocked_stop_bridge.assert_called_once_with()
        self.assertEqual(["Hub"], [call.args[0] for call in mocked_start.call_args_list])
        mocked_start_onebot.assert_called_once_with(env=None)
        mocked_start_qq_bridge.assert_called_once_with(env=None)

    def test_restart_all_restarts_only_weixin_stack(self) -> None:
        with (
            patch("runtime_stack.stop_managed", side_effect=["Bridge stopped", "Hub stopped"]) as mocked_stop,
            patch("runtime_stack.start_managed", side_effect=["Hub started", "Bridge started"]) as mocked_start,
            patch("runtime_stack.stop_qq_bridge", return_value="QQ Bridge stopped") as mocked_stop_qq_bridge,
            patch("runtime_stack.stop_onebot_runtime", return_value="OneBot stopped") as mocked_stop_onebot,
            patch("runtime_stack.time.sleep"),
            patch("runtime_stack._managed_subprocess_env", return_value={"A": "B"}),
        ):
            messages = restart_all()

        self.assertEqual(["QQ Bridge stopped", "OneBot stopped", "Bridge stopped", "Hub stopped", "Hub started", "Bridge started"], messages)
        self.assertEqual(["Bridge", "Hub"], [call.args[0] for call in mocked_stop.call_args_list])
        self.assertEqual(["Hub", "Bridge"], [call.args[0] for call in mocked_start.call_args_list])
        mocked_stop_qq_bridge.assert_called_once_with()
        mocked_stop_onebot.assert_called_once_with()

    def test_restart_qq_bridge_preserves_onebot_runtime(self) -> None:
        with (
            patch("runtime_stack.stop_bridge", return_value="Bridge stopped") as mocked_stop_bridge,
            patch("runtime_stack.stop_qq_bridge", return_value="QQ Bridge stopped") as mocked_stop_qq_bridge,
            patch("runtime_stack.start_qq_bridge", return_value="QQ Bridge started") as mocked_start_qq_bridge,
            patch("runtime_stack.stop_onebot_runtime") as mocked_stop_onebot,
            patch("runtime_stack.start_onebot_runtime") as mocked_start_onebot,
            patch("runtime_stack._managed_subprocess_env", return_value={"A": "B"}),
        ):
            messages = restart_qq_bridge()

        self.assertEqual(["Bridge stopped", "QQ Bridge stopped", "QQ Bridge started"], messages)
        mocked_stop_bridge.assert_called_once_with()
        mocked_stop_qq_bridge.assert_called_once_with()
        mocked_start_qq_bridge.assert_called_once_with(env={"A": "B"})
        mocked_stop_onebot.assert_not_called()
        mocked_start_onebot.assert_not_called()

    def test_restart_qq_stack_restarts_hub_and_qq_bridge_preserving_onebot_runtime(self) -> None:
        with (
            patch("runtime_stack.stop_bridge", return_value="Bridge stopped") as mocked_stop_bridge,
            patch("runtime_stack.stop_qq_bridge", return_value="QQ Bridge stopped") as mocked_stop_qq_bridge,
            patch("runtime_stack.stop_managed", return_value="Hub stopped") as mocked_stop_hub,
            patch("runtime_stack.start_managed", return_value="Hub started") as mocked_start_hub,
            patch("runtime_stack.start_qq_bridge", return_value="QQ Bridge started") as mocked_start_qq_bridge,
            patch("runtime_stack.stop_onebot_runtime") as mocked_stop_onebot,
            patch("runtime_stack.start_onebot_runtime") as mocked_start_onebot,
            patch("runtime_stack.time.sleep"),
            patch("runtime_stack._managed_subprocess_env", return_value={"A": "B"}),
        ):
            messages = restart_qq_stack()

        self.assertEqual(["Bridge stopped", "QQ Bridge stopped", "Hub stopped", "Hub started", "QQ Bridge started"], messages)
        mocked_stop_bridge.assert_called_once_with()
        mocked_stop_qq_bridge.assert_called_once_with()
        self.assertEqual(["Hub"], [call.args[0] for call in mocked_stop_hub.call_args_list])
        self.assertEqual(["Hub"], [call.args[0] for call in mocked_start_hub.call_args_list])
        mocked_start_qq_bridge.assert_called_once_with(env={"A": "B"})
        mocked_stop_onebot.assert_not_called()
        mocked_start_onebot.assert_not_called()

    def test_restart_hub_does_not_touch_bridges_or_onebot(self) -> None:
        with (
            patch("runtime_stack.stop_managed", return_value="Hub stopped") as mocked_stop_hub,
            patch("runtime_stack.start_managed", return_value="Hub started") as mocked_start_hub,
            patch("runtime_stack.stop_bridge") as mocked_stop_bridge,
            patch("runtime_stack.stop_qq_bridge") as mocked_stop_qq_bridge,
            patch("runtime_stack.stop_onebot_runtime") as mocked_stop_onebot,
            patch("runtime_stack._managed_subprocess_env", return_value={"A": "B"}),
        ):
            messages = restart_hub()

        self.assertEqual(["Hub stopped", "Hub started"], messages)
        self.assertEqual(["Hub"], [call.args[0] for call in mocked_stop_hub.call_args_list])
        self.assertEqual(["Hub"], [call.args[0] for call in mocked_start_hub.call_args_list])
        mocked_stop_bridge.assert_not_called()
        mocked_stop_qq_bridge.assert_not_called()
        mocked_stop_onebot.assert_not_called()
        mocked_start_hub.assert_called_once()
        self.assertEqual({"A": "B"}, mocked_start_hub.call_args.kwargs["env"])

    def test_managed_subprocess_env_copies_proxy_from_running_process(self) -> None:
        fake_proc = SimpleNamespace(pid=123)
        with patch.dict("runtime_stack.os.environ", {}, clear=True):
            with patch("runtime_stack._discover_windows_user_env", return_value={}):
                with patch("runtime_stack._find_processes_by_script", return_value=[fake_proc]):
                    with patch("runtime_stack._read_process_proxy_env", return_value={"HTTPS_PROXY": "http://127.0.0.1:7890"}):
                        env = _managed_subprocess_env({})
        self.assertEqual("http://127.0.0.1:7890", env["HTTPS_PROXY"])

    def test_managed_subprocess_env_fills_missing_windows_user_variables(self) -> None:
        with (
            patch("runtime_stack._discover_windows_user_env", return_value={"NINE_ROUTER_API_KEY": "secret-value"}),
            patch("runtime_stack._discover_proxy_env", return_value={}),
        ):
            env = _managed_subprocess_env({"PATH": "C:/tools"})

        self.assertEqual("secret-value", env["NINE_ROUTER_API_KEY"])

    def test_managed_subprocess_env_preserves_existing_values(self) -> None:
        with (
            patch("runtime_stack._discover_windows_user_env", return_value={"NINE_ROUTER_API_KEY": "user-value"}),
            patch("runtime_stack._discover_proxy_env", return_value={}),
        ):
            env = _managed_subprocess_env({"NINE_ROUTER_API_KEY": "process-value"})

        self.assertEqual("process-value", env["NINE_ROUTER_API_KEY"])

    def test_onebot_runtime_env_sets_quick_login_from_napcat_config(self) -> None:
        with patch("runtime_stack._managed_subprocess_env", return_value={}):
            with patch("runtime_stack._detect_napcat_quick_login_uin", return_value="900000001"):
                env = _onebot_runtime_env({})

        self.assertEqual("900000001", env["CHATBRIDGE_NAPCAT_QQ"])

    def test_onebot_runtime_env_isolates_windows_qq_profile(self) -> None:
        profile_root = self.root / "onebot-runtime" / "qq-profile"
        with (
            patch("runtime_stack.IS_WINDOWS", True),
            patch("runtime_stack.ONEBOT_RUNTIME_DIR", self.root / "onebot-runtime"),
            patch("runtime_stack._managed_subprocess_env", return_value={}),
            patch("runtime_stack._detect_napcat_quick_login_uin", return_value=""),
        ):
            env = _onebot_runtime_env({})

        self.assertEqual(str(profile_root / "User"), env["USERPROFILE"])
        self.assertEqual(str(profile_root / "User" / "AppData" / "Roaming"), env["APPDATA"])
        self.assertEqual(str(profile_root / "User" / "AppData" / "Local"), env["LOCALAPPDATA"])
        self.assertTrue((profile_root / "User" / "AppData" / "Roaming").is_dir())
        self.assertTrue((profile_root / "User" / "AppData" / "Local").is_dir())

    def test_onebot_runtime_env_allows_custom_profile_root(self) -> None:
        profile_root = self.root / "custom-profile"
        with (
            patch("runtime_stack.IS_WINDOWS", True),
            patch("runtime_stack._managed_subprocess_env", return_value={"CHATBRIDGE_ONEBOT_PROFILE_ROOT": str(profile_root)}),
            patch("runtime_stack._detect_napcat_quick_login_uin", return_value=""),
        ):
            env = _onebot_runtime_env({})

        self.assertEqual(str(profile_root.resolve() / "User" / "AppData" / "Roaming"), env["APPDATA"])

    def test_onebot_runtime_env_can_disable_profile_isolation(self) -> None:
        with (
            patch("runtime_stack.IS_WINDOWS", True),
            patch("runtime_stack._managed_subprocess_env", return_value={"CHATBRIDGE_ONEBOT_ISOLATE_QQ_PROFILE": "0", "APPDATA": "C:/Users/me/AppData/Roaming"}),
            patch("runtime_stack._detect_napcat_quick_login_uin", return_value=""),
        ):
            env = _onebot_runtime_env({})

        self.assertEqual("C:/Users/me/AppData/Roaming", env["APPDATA"])
        self.assertNotIn("LOCALAPPDATA", env)

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
                with patch("runtime_stack._wait_for_script_processes_to_exit", return_value=[]):
                    with patch("runtime_stack._clear_pid_file") as mocked_clear:
                        message = stop_managed("Bridge", self.root / "weixin_hub_bridge.py", pid_file)
        self.assertEqual([(101,), (202,)], [call.args for call in mocked_kill.call_args_list])
        mocked_clear.assert_called_once_with(pid_file)
        self.assertIn("PIDs 101, 202", message)

    def test_stop_onebot_runtime_stops_marker_and_port_processes(self) -> None:
        marker_proc = SimpleNamespace(pid=101)
        port_proc = SimpleNamespace(pid=202)
        with (
            patch("runtime_stack._find_processes_by_markers", return_value=[marker_proc]),
            patch("runtime_stack._find_onebot_runtime_port_processes", return_value=[marker_proc, port_proc]),
            patch("runtime_stack._taskkill") as mocked_kill,
            patch("runtime_stack._clear_pid_file") as mocked_clear,
        ):
            message = stop_onebot_runtime()

        self.assertEqual([(101,), (202,)], [call.args for call in mocked_kill.call_args_list])
        mocked_clear.assert_called_once()
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
        with patch("runtime_stack.IS_WINDOWS", False), patch("runtime_stack.psutil", object()):
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
        with patch("runtime_stack.IS_WINDOWS", False), patch("runtime_stack.psutil", fake_psutil):
            with patch("runtime_stack._get_process", return_value=proc):
                _taskkill(123)
        self.assertTrue(proc.terminated)
        self.assertTrue(proc.killed)
        self.assertTrue(child.terminated)
        self.assertTrue(child.killed)
