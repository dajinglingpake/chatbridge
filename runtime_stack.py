from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

try:
    import psutil
except ImportError:  # pragma: no cover - optional dependency
    psutil = None

from core.json_store import load_json, save_json
from core.platform_compat import IS_WINDOWS, creationflags
from core.runtime_paths import (
    APP_DIR,
    BRIDGE_CONVERSATIONS_PATH,
    BRIDGE_ERR_LOG,
    BRIDGE_OUT_LOG,
    BRIDGE_PID_FILE,
    BRIDGE_STATE_PATH,
    HUB_ERR_LOG,
    HUB_OUT_LOG,
    HUB_PID_FILE,
    HUB_STATE_PATH,
    LOG_DIR,
    RUNTIME_DIR,
    SESSION_DIR,
    STATE_DIR,
    WORKSPACE_DIR,
)
from core.state_models import ExternalAgentProcessState, RuntimeSnapshot

HUB_SCRIPT = APP_DIR / "agent_hub.py"
BRIDGE_SCRIPT = APP_DIR / "weixin_hub_bridge.py"
PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy")

@dataclass
class ManagedStatus:
    name: str
    script_path: Path
    pid_file: Path
    running: bool
    pid: int | None = None


def _normalize_process_text(name: str, cmdline: str) -> str:
    return f"{name} {cmdline}".lower()


def infer_agent_backend(name: str, cmdline: str) -> str:
    lowered = _normalize_process_text(name, cmdline)
    if "claude" in lowered:
        return "claude"
    if "opencode" in lowered:
        return "opencode"
    return "codex"


def extract_agent_session_hint(cmdline: str) -> str:
    try:
        parts = shlex.split(cmdline)
    except ValueError:
        parts = cmdline.split()
    for index, part in enumerate(parts):
        if part in {"resume", "--resume", "--session"} and index + 1 < len(parts):
            return parts[index + 1]
    return ""


def ensure_runtime_dirs() -> None:
    for path in [RUNTIME_DIR, STATE_DIR, LOG_DIR, SESSION_DIR, WORKSPACE_DIR]:
        path.mkdir(parents=True, exist_ok=True)


def _read_pid_file(path: Path) -> int | None:
    data = load_json(path, None)
    if data is None:
        return None
    try:
        return int(data)
    except (TypeError, ValueError):
        return None


def _write_pid_file(path: Path, pid: int) -> None:
    save_json(path, pid)


def _clear_pid_file(path: Path) -> None:
    path.unlink(missing_ok=True)


def _get_process(pid: int):
    if psutil is None:
        return None
    try:
        return psutil.Process(pid)
    except (psutil.Error, ProcessLookupError):
        return None


def _cmdline_text(proc) -> str:
    if psutil is None:
        return ""
    try:
        return " ".join(proc.cmdline())
    except (psutil.Error, OSError):
        return ""


def _find_process_by_script(script_path: Path):
    matches = _find_processes_by_script(script_path)
    return matches[0] if matches else None


def _find_processes_by_script(script_path: Path) -> list[object]:
    if psutil is None:
        return []
    target = str(script_path)
    matches: list[object] = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        cmdline = proc.info.get("cmdline") or []
        if target in cmdline or target in " ".join(cmdline):
            matches.append(proc)
    return sorted(matches, key=lambda item: getattr(item, "pid", 0))


def _managed_root_pids() -> set[int]:
    managed: set[int] = set()
    for status in [
        get_managed_status("Hub", HUB_SCRIPT, HUB_PID_FILE),
        get_managed_status("Bridge", BRIDGE_SCRIPT, BRIDGE_PID_FILE),
    ]:
        if status.running and status.pid:
            managed.add(status.pid)
    return managed


def _has_managed_ancestor(proc, managed_root_pids: set[int]) -> bool:
    if psutil is None or not managed_root_pids:
        return False
    try:
        for parent in proc.parents():
            if parent.pid in managed_root_pids:
                return True
    except psutil.Error:
        return False
    return False


def discover_external_agent_processes() -> list[ExternalAgentProcessState]:
    if psutil is None:
        return []

    current_pid = os.getpid()
    managed_root_pids = _managed_root_pids()
    rendered: list[ExternalAgentProcessState] = []
    parent_map: dict[int, int | None] = {}
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        pid = proc.info.get("pid")
        if pid in {None, current_pid}:
            continue
        if pid in managed_root_pids:
            continue
        if _has_managed_ancestor(proc, managed_root_pids):
            continue
        cmdline = " ".join(proc.info.get("cmdline") or [])
        name = str(proc.info.get("name") or "")
        lowered = _normalize_process_text(name, cmdline)
        if "codex" not in lowered and "claude" not in lowered and "opencode" not in lowered:
            continue
        try:
            parent_map[int(pid)] = proc.ppid()
        except psutil.Error:
            parent_map[int(pid)] = None
        rendered.append(
            ExternalAgentProcessState(
                pid=int(pid),
                name=name,
                backend=infer_agent_backend(name, cmdline),
                session_hint=extract_agent_session_hint(cmdline),
                command_line=cmdline,
            )
        )
    candidate_pids = {item.pid for item in rendered}
    parent_candidates = {parent_pid for parent_pid in parent_map.values() if parent_pid in candidate_pids}
    filtered: list[ExternalAgentProcessState] = []
    for item in rendered:
        if item.pid in parent_candidates:
            continue
        filtered.append(item)
    return sorted(filtered, key=lambda item: item.pid)


def stop_external_agent_process(pid: int) -> str:
    if pid <= 0:
        return "结束失败：PID 无效"
    known_pids = {item.pid for item in discover_external_agent_processes()}
    if pid not in known_pids:
        return f"结束失败：PID {pid} 不是可控的外部 Agent 进程"
    _taskkill(pid)
    if psutil is not None:
        proc = _get_process(pid)
        if proc is not None:
            return f"结束失败：PID {pid} 仍在运行"
    return f"已结束外部 Agent 进程 PID {pid}"


def get_managed_status(name: str, script_path: Path, pid_file: Path) -> ManagedStatus:
    pid = _read_pid_file(pid_file)
    proc = _get_process(pid) if pid else None
    if proc and str(script_path) in _cmdline_text(proc):
        return ManagedStatus(name=name, script_path=script_path, pid_file=pid_file, running=True, pid=proc.pid)

    discovered = _find_process_by_script(script_path)
    if discovered:
        _write_pid_file(pid_file, discovered.pid)
        return ManagedStatus(name=name, script_path=script_path, pid_file=pid_file, running=True, pid=discovered.pid)

    _clear_pid_file(pid_file)
    return ManagedStatus(name=name, script_path=script_path, pid_file=pid_file, running=False, pid=None)


def _get_python_command(gui: bool = False) -> str:
    if gui:
        pythonw = shutil_which("pythonw")
        if pythonw:
            return pythonw
    if sys.executable:
        return sys.executable
    python = shutil_which("python")
    if python:
        return python
    return sys.executable


def shutil_which(name: str) -> str | None:
    return shutil.which(name)


def _read_process_proxy_env(pid: int) -> dict[str, str]:
    if IS_WINDOWS:
        return {}
    try:
        raw_values = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return {}
    proxy_keys = {key.lower() for key in PROXY_ENV_KEYS}
    values: dict[str, str] = {}
    for raw in raw_values:
        if b"=" not in raw:
            continue
        key_raw, value_raw = raw.split(b"=", 1)
        key = key_raw.decode("utf-8", errors="replace")
        if key.lower() not in proxy_keys:
            continue
        value = value_raw.decode("utf-8", errors="replace").strip()
        if value:
            values[key] = value
    return values


def _discover_proxy_env() -> dict[str, str]:
    values = {key: value for key in PROXY_ENV_KEYS if (value := os.environ.get(key, "").strip())}
    for script_path in (HUB_SCRIPT, BRIDGE_SCRIPT):
        for proc in _find_processes_by_script(script_path):
            for key, value in _read_process_proxy_env(proc.pid).items():
                values.setdefault(key, value)
    return values


def _managed_subprocess_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(base_env or os.environ)
    for key, value in _discover_proxy_env().items():
        if not env.get(key, "").strip():
            env[key] = value
    return env


def start_managed(
    name: str,
    script_path: Path,
    pid_file: Path,
    stdout_log: Path,
    stderr_log: Path,
    *,
    env: dict[str, str] | None = None,
) -> str:
    running = _find_processes_by_script(script_path)
    if running:
        primary = running[0]
        duplicate_pids: list[int] = []
        for proc in running[1:]:
            duplicate_pids.append(proc.pid)
            _taskkill(proc.pid)
        _write_pid_file(pid_file, primary.pid)
        if duplicate_pids:
            rendered = ", ".join(str(pid) for pid in duplicate_pids)
            return f"{name} already running (PID {primary.pid}); cleaned duplicate PIDs {rendered}"
        return f"{name} already running (PID {primary.pid})"

    ensure_runtime_dirs()
    python_cmd = _get_python_command(gui=False)
    with stdout_log.open("ab") as out_handle, stderr_log.open("ab") as err_handle:
        proc = subprocess.Popen(
            [python_cmd, str(script_path)],
            cwd=str(APP_DIR),
            stdout=out_handle,
            stderr=err_handle,
            env=_managed_subprocess_env(env),
            creationflags=creationflags(),
            start_new_session=not IS_WINDOWS,
        )
    _write_pid_file(pid_file, proc.pid)
    return f"{name} started (PID {proc.pid})"


def _taskkill(pid: int) -> None:
    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags(),
            check=False,
        )
        return

    if psutil is not None:
        proc = _get_process(pid)
        if proc is None:
            return
        current_pid = os.getpid()
        children = proc.children(recursive=True)

        def safe_wait(process: object, timeout: float) -> bool:
            try:
                process.wait(timeout=timeout)
                return True
            except (psutil.Error, TimeoutError, OSError):
                return False

        def safe_kill(process: object) -> None:
            try:
                process.kill()
            except (psutil.Error, OSError):
                pass

        for child in children:
            if child.pid == current_pid:
                continue
            try:
                child.terminate()
            except (psutil.Error, OSError):
                pass
        try:
            proc.terminate()
        except (psutil.Error, OSError):
            pass
        if not safe_wait(proc, 5):
            safe_kill(proc)
            safe_wait(proc, 2)
        for child in children:
            if child.pid == current_pid:
                continue
            if not safe_wait(child, 2):
                safe_kill(child)
                safe_wait(child, 1)
        return

    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.5)
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    except OSError:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def stop_managed(name: str, script_path: Path, pid_file: Path) -> str:
    running = _find_processes_by_script(script_path)
    if not running:
        _clear_pid_file(pid_file)
        return f"{name} is not running"
    stopped_pids: list[int] = []
    for proc in running:
        stopped_pids.append(proc.pid)
        _taskkill(proc.pid)
    _clear_pid_file(pid_file)
    if len(stopped_pids) == 1:
        return f"{name} stopped (PID {stopped_pids[0]})"
    rendered = ", ".join(str(pid) for pid in stopped_pids)
    return f"{name} stopped (PIDs {rendered})"


def start_all(*, env: dict[str, str] | None = None) -> list[str]:
    messages = [start_managed("Hub", HUB_SCRIPT, HUB_PID_FILE, HUB_OUT_LOG, HUB_ERR_LOG, env=env)]
    time.sleep(1.5)
    messages.append(start_managed("Bridge", BRIDGE_SCRIPT, BRIDGE_PID_FILE, BRIDGE_OUT_LOG, BRIDGE_ERR_LOG, env=env))
    return messages


def start_bridge(*, env: dict[str, str] | None = None) -> str:
    return start_managed("Bridge", BRIDGE_SCRIPT, BRIDGE_PID_FILE, BRIDGE_OUT_LOG, BRIDGE_ERR_LOG, env=env)


def stop_bridge() -> str:
    return stop_managed("Bridge", BRIDGE_SCRIPT, BRIDGE_PID_FILE)


def restart_bridge() -> list[str]:
    env = _managed_subprocess_env()
    return [stop_bridge(), start_bridge(env=env)]


def stop_all() -> list[str]:
    messages = [stop_managed("Bridge", BRIDGE_SCRIPT, BRIDGE_PID_FILE)]
    messages.append(stop_managed("Hub", HUB_SCRIPT, HUB_PID_FILE))
    return messages


def restart_all() -> list[str]:
    env = _managed_subprocess_env()
    messages = stop_all()
    messages.extend(start_all(env=env))
    return messages


def emergency_stop() -> list[str]:
    messages = stop_all()
    if psutil is not None:
        targets: list[int] = []
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            cmdline = " ".join(proc.info.get("cmdline") or [])
            name = (proc.info.get("name") or "").lower()
            lowered = cmdline.lower()
            if (
                "codex" in lowered
                or "claude" in lowered
                or "opencode" in lowered
                or name.startswith("codex")
                or name.startswith("claude")
                or name.startswith("opencode")
            ):
                targets.append(proc.info["pid"])
        for pid in sorted(set(targets)):
            _taskkill(pid)
        if targets:
            messages.append(f"Agent child processes killed: {len(set(targets))}")
    return messages


def list_codex_processes() -> list[str]:
    if psutil is None:
        return ["psutil missing; agent process discovery is unavailable"]

    rendered: list[str] = []
    current_pid = os.getpid()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        pid = proc.info.get("pid")
        if pid == current_pid:
            continue
        cmdline = " ".join(proc.info.get("cmdline") or [])
        name = (proc.info.get("name") or "").lower()
        lowered = cmdline.lower()
        if (
            "codex" not in lowered
            and "claude" not in lowered
            and "opencode" not in lowered
            and not name.startswith("codex")
            and not name.startswith("claude")
            and not name.startswith("opencode")
        ):
            continue
        rendered.append(f"PID {pid} :: {cmdline or name}")
    return sorted(rendered)


def read_json(path: Path) -> dict:
    data = load_json(path, {}, expect_type=dict)
    return data if isinstance(data, dict) else {}


def get_runtime_snapshot() -> RuntimeSnapshot:
    hub = get_managed_status("Hub", HUB_SCRIPT, HUB_PID_FILE)
    bridge = get_managed_status("Bridge", BRIDGE_SCRIPT, BRIDGE_PID_FILE)
    return RuntimeSnapshot(
        hub_running=hub.running,
        bridge_running=bridge.running,
        hub_pid=hub.pid,
        bridge_pid=bridge.pid,
        codex_processes=list_codex_processes(),
        log_dir=str(LOG_DIR),
    )
