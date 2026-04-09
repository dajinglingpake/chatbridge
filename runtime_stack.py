from __future__ import annotations

import json
import os
import shlex
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

from core.platform_compat import IS_WINDOWS, creationflags


APP_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = APP_DIR / ".runtime"
STATE_DIR = RUNTIME_DIR / "state"
LOG_DIR = RUNTIME_DIR / "logs"
SESSION_DIR = APP_DIR / "sessions"
WORKSPACE_DIR = APP_DIR / "workspace"

HUB_SCRIPT = APP_DIR / "agent_hub.py"
BRIDGE_SCRIPT = APP_DIR / "weixin_hub_bridge.py"
HUB_PID_FILE = RUNTIME_DIR / "agent_hub.pid"
BRIDGE_PID_FILE = RUNTIME_DIR / "weixin_hub_bridge.pid"
HUB_OUT_LOG = LOG_DIR / "agent_hub.out.log"
HUB_ERR_LOG = LOG_DIR / "agent_hub.err.log"
BRIDGE_OUT_LOG = LOG_DIR / "weixin_hub_bridge.out.log"
BRIDGE_ERR_LOG = LOG_DIR / "weixin_hub_bridge.err.log"
HUB_STATE_PATH = STATE_DIR / "agent_hub_state.json"
BRIDGE_STATE_PATH = STATE_DIR / "weixin_hub_bridge_state.json"
BRIDGE_CONVERSATIONS_PATH = STATE_DIR / "weixin_conversations.json"

@dataclass
class ManagedStatus:
    name: str
    script_path: Path
    pid_file: Path
    running: bool
    pid: int | None = None


@dataclass
class RuntimeSnapshot:
    hub_running: bool
    bridge_running: bool
    hub_pid: int | None
    bridge_pid: int | None
    codex_processes: list[str]
    log_dir: str


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
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _write_pid_file(path: Path, pid: int) -> None:
    path.write_text(str(pid), encoding="utf-8")


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
    if psutil is None:
        return None
    target = str(script_path)
    for proc in psutil.process_iter(["pid", "cmdline"]):
        cmdline = proc.info.get("cmdline") or []
        if target in cmdline or target in " ".join(cmdline):
            return proc
    return None


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


def discover_external_agent_processes() -> list[dict[str, str | int]]:
    if psutil is None:
        return []

    current_pid = os.getpid()
    managed_root_pids = _managed_root_pids()
    rendered: list[dict[str, str | int]] = []
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
            {
                "pid": int(pid),
                "name": name,
                "backend": infer_agent_backend(name, cmdline),
                "session_hint": extract_agent_session_hint(cmdline),
                "command_line": cmdline,
            }
        )
    candidate_pids = {int(item["pid"]) for item in rendered}
    parent_candidates = {parent_pid for parent_pid in parent_map.values() if parent_pid in candidate_pids}
    filtered: list[dict[str, str | int]] = []
    for item in rendered:
        pid = int(item["pid"])
        if pid in parent_candidates:
            continue
        filtered.append(item)
    return sorted(filtered, key=lambda item: int(item["pid"]))


def stop_external_agent_process(pid: int) -> str:
    if pid <= 0:
        return "结束失败：PID 无效"
    known_pids = {int(item["pid"]) for item in discover_external_agent_processes()}
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
    python = shutil_which("python")
    if python:
        return python
    return sys.executable


def shutil_which(name: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / name
        if candidate.exists():
            return str(candidate)
    return None


def start_managed(name: str, script_path: Path, pid_file: Path, stdout_log: Path, stderr_log: Path) -> str:
    status = get_managed_status(name, script_path, pid_file)
    if status.running:
        return f"{name} already running (PID {status.pid})"

    ensure_runtime_dirs()
    python_cmd = _get_python_command(gui=False)
    with stdout_log.open("ab") as out_handle, stderr_log.open("ab") as err_handle:
        proc = subprocess.Popen(
            [python_cmd, str(script_path)],
            cwd=str(APP_DIR),
            stdout=out_handle,
            stderr=err_handle,
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
        children = proc.children(recursive=True)
        for child in children:
            try:
                child.terminate()
            except psutil.Error:
                pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except (psutil.Error, TimeoutError):
            try:
                proc.kill()
            except psutil.Error:
                pass
        for child in children:
            try:
                child.wait(timeout=2)
            except psutil.Error:
                try:
                    child.kill()
                except psutil.Error:
                    pass
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
    status = get_managed_status(name, script_path, pid_file)
    if not status.running or not status.pid:
        return f"{name} is not running"
    _taskkill(status.pid)
    _clear_pid_file(pid_file)
    return f"{name} stopped (PID {status.pid})"


def start_all() -> list[str]:
    messages = [start_managed("Hub", HUB_SCRIPT, HUB_PID_FILE, HUB_OUT_LOG, HUB_ERR_LOG)]
    time.sleep(1.5)
    messages.append(start_managed("Bridge", BRIDGE_SCRIPT, BRIDGE_PID_FILE, BRIDGE_OUT_LOG, BRIDGE_ERR_LOG))
    return messages


def start_bridge() -> str:
    return start_managed("Bridge", BRIDGE_SCRIPT, BRIDGE_PID_FILE, BRIDGE_OUT_LOG, BRIDGE_ERR_LOG)


def stop_bridge() -> str:
    return stop_managed("Bridge", BRIDGE_SCRIPT, BRIDGE_PID_FILE)


def restart_bridge() -> list[str]:
    return [stop_bridge(), start_bridge()]


def stop_all() -> list[str]:
    messages = [stop_managed("Bridge", BRIDGE_SCRIPT, BRIDGE_PID_FILE)]
    messages.append(stop_managed("Hub", HUB_SCRIPT, HUB_PID_FILE))
    return messages


def restart_all() -> list[str]:
    messages = stop_all()
    messages.extend(start_all())
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
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


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
