from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

try:
    import psutil
except ImportError:  # pragma: no cover - optional dependency
    psutil = None

from core.json_store import load_json, save_json
from core.onebot_runtime_installer import NAPCAT_QUICK_LOGIN_ENV, NAPCAT_RUNTIME_NAME, NAPCAT_WEBUI_TOKEN, ensure_default_onebot_runtime, fetch_napcat_login_info, fetch_napcat_login_status, find_installed_napcat_launcher
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
    ONEBOT_RUNTIME_ERR_LOG,
    ONEBOT_RUNTIME_OUT_LOG,
    ONEBOT_RUNTIME_PID_FILE,
    ONEBOT_RUNTIME_DIR,
    QQ_BRIDGE_ERR_LOG,
    QQ_BRIDGE_OUT_LOG,
    QQ_BRIDGE_PID_FILE,
    RUNTIME_DIR,
    SESSION_DIR,
    STATE_DIR,
    WORKSPACE_DIR,
)
from core.state_models import ExternalAgentProcessState, RuntimeSnapshot

HUB_SCRIPT = APP_DIR / "agent_hub.py"
BRIDGE_SCRIPT = APP_DIR / "weixin_hub_bridge.py"
QQ_BRIDGE_SCRIPT = APP_DIR / "qq_onebot_bridge.py"
ONEBOT_RUNTIME_COMMAND_ENV = "CHATBRIDGE_ONEBOT_RUNTIME_COMMAND"
ONEBOT_PROFILE_ROOT_ENV = "CHATBRIDGE_ONEBOT_PROFILE_ROOT"
ONEBOT_PROFILE_ISOLATION_ENV = "CHATBRIDGE_ONEBOT_ISOLATE_QQ_PROFILE"
ONEBOT_API_BASE = "http://127.0.0.1:3000"
ONEBOT_RUNTIME_PROCESS_MARKERS = ("llonebot", "lagrange.onebot", "lagrange-onebot", "chatbridge-start-lagrange", "chatbridge-start-napcat", "napcat", "napcatqq", "go-cqhttp")
ONEBOT_RUNTIME_PORTS = {3000, 6099, 6100, 6101, 6102}
PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy")
AGENT_PROCESS_KEYWORDS = ("codex", "claude", "opencode")
QQ_LOGIN_STATUS_LOG = LOG_DIR / "qq_login_status.jsonl"
_QQ_LOGIN_STATUS_AUDIT_LAST_SIGNATURE: tuple[object, ...] | None = None
_QQ_LOGIN_STATUS_LAST_CHECKED: tuple[str, str] | None = None
_SYSTEM_RESOURCE_CACHE_SECONDS = 1.0
_SYSTEM_RESOURCE_CACHE: tuple[float, dict[str, float | int | None]] | None = None
AGENT_PROCESS_HOST_NAMES = {
    "bash",
    "bash.exe",
    "cmd",
    "cmd.exe",
    "node",
    "node.exe",
    "npm",
    "npm.cmd",
    "npx",
    "npx.cmd",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
    "python",
    "python.exe",
    "pythonw",
    "pythonw.exe",
    "sh",
    "sh.exe",
    "wsl",
    "wsl.exe",
}


def get_system_resource_usage(*, force: bool = False) -> dict[str, float | int | None]:
    global _SYSTEM_RESOURCE_CACHE
    now = time.monotonic()
    if not force and _SYSTEM_RESOURCE_CACHE is not None:
        cached_at, cached_usage = _SYSTEM_RESOURCE_CACHE
        if now - cached_at <= _SYSTEM_RESOURCE_CACHE_SECONDS:
            return dict(cached_usage)

    usage: dict[str, float | int | None] = {
        "cpu_percent": None,
        "memory_percent": None,
        "memory_used_bytes": None,
        "memory_total_bytes": None,
    }
    if psutil is not None:
        try:
            memory = psutil.virtual_memory()
            usage = {
                "cpu_percent": round(max(0.0, min(100.0, float(psutil.cpu_percent(interval=None)))), 1),
                "memory_percent": round(max(0.0, min(100.0, float(memory.percent))), 1),
                "memory_used_bytes": max(0, int(memory.used)),
                "memory_total_bytes": max(0, int(memory.total)),
            }
        except Exception:
            pass
    _SYSTEM_RESOURCE_CACHE = (now, usage)
    return dict(usage)

@dataclass
class ManagedStatus:
    name: str
    script_path: Path
    pid_file: Path
    running: bool
    pid: int | None = None


def _normalize_process_text(name: str, cmdline: str) -> str:
    return f"{name} {cmdline}".lower()


def _is_agent_process_name(name: str) -> bool:
    lowered = name.lower()
    return any(keyword in lowered for keyword in AGENT_PROCESS_KEYWORDS)


def _is_agent_process_host_name(name: str) -> bool:
    lowered = name.lower()
    return _is_agent_process_name(lowered) or lowered in AGENT_PROCESS_HOST_NAMES


def _iter_agent_candidate_processes():
    if psutil is None:
        return
    for proc in psutil.process_iter(["pid", "name"]):
        name = str(proc.info.get("name") or "")
        if _is_agent_process_host_name(name):
            yield proc


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
    targets = {str(script_path).lower(), str(script_path).replace("\\", "/").lower()}
    matches: list[object] = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        cmdline = proc.info.get("cmdline") or []
        joined = " ".join(str(item) for item in cmdline).lower()
        normalized_joined = joined.replace("\\", "/")
        if any(target in joined or target in normalized_joined for target in targets):
            matches.append(proc)
    return _filter_child_process_matches(sorted(matches, key=lambda item: getattr(item, "pid", 0)))


def _process_matches_script(proc: object, script_path: Path) -> bool:
    """Match a managed script independent of Windows path separator style."""
    target = str(script_path).replace("\\", "/").lower()
    try:
        target = str(script_path.resolve()).replace("\\", "/").lower()
    except OSError:
        pass
    cmdline = _cmdline_text(proc).replace("\\", "/").lower()
    return target in cmdline or str(script_path).replace("\\", "/").lower() in cmdline


def _filter_child_process_matches(processes: list[object]) -> list[object]:
    matched_pids = {getattr(proc, "pid", None) for proc in processes}
    filtered: list[object] = []
    for proc in processes:
        try:
            parent_pid = proc.ppid()
        except (AttributeError, psutil.Error, OSError):
            parent_pid = None
        if parent_pid in matched_pids:
            continue
        filtered.append(proc)
    return filtered


def _find_processes_by_markers(markers: tuple[str, ...]) -> list[object]:
    if psutil is None:
        return []
    lowered_markers = tuple(marker.lower() for marker in markers)
    launcher_markers = (
        "llonebot",
        "lagrange.onebot",
        "lagrange-onebot",
        "chatbridge-start-lagrange",
        "chatbridge-start-napcat",
        "napcat.bat",
        "napcatwinbootmain",
        "napcat.mjs",
        "napcat.shell",
        "go-cqhttp",
    )
    current_pid = os.getpid()
    matches: list[object] = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        if proc.info.get("pid") == current_pid:
            continue
        name = str(proc.info.get("name") or "")
        cmdline = " ".join(proc.info.get("cmdline") or [])
        lowered_name = name.lower()
        lowered_cmdline = cmdline.lower()
        if any(marker in lowered_name for marker in lowered_markers) or any(marker in lowered_cmdline for marker in launcher_markers):
            matches.append(proc)
    return sorted(matches, key=lambda item: getattr(item, "pid", 0))

def _find_onebot_runtime_port_processes() -> list[object]:
    if psutil is None:
        return []
    pids: set[int] = set()
    for conn in psutil.net_connections(kind="tcp"):
        try:
            if conn.status == psutil.CONN_LISTEN and conn.laddr.port in ONEBOT_RUNTIME_PORTS and conn.pid:
                pids.add(int(conn.pid))
        except (AttributeError, TypeError):
            continue
    processes: dict[int, object] = {}
    for pid in pids:
        proc = _get_process(pid)
        if proc is None:
            continue
        processes[pid] = proc
        cmdline = _cmdline_text(proc).lower()
        try:
            parent = proc.parent()
        except (psutil.Error, OSError):
            parent = None
        if parent is not None and "node.mojom.nodeservice" in cmdline and "--enable-logging" in _cmdline_text(parent).lower():
            processes[parent.pid] = parent
    return sorted(processes.values(), key=lambda item: getattr(item, "pid", 0))


def _managed_root_pids() -> set[int]:
    managed: set[int] = set()
    for status in [
        get_managed_status("Hub", HUB_SCRIPT, HUB_PID_FILE),
        get_managed_status("Bridge", BRIDGE_SCRIPT, BRIDGE_PID_FILE),
        get_managed_status("QQ Bridge", QQ_BRIDGE_SCRIPT, QQ_BRIDGE_PID_FILE),
        get_onebot_runtime_status(),
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
    for proc in _iter_agent_candidate_processes():
        pid = proc.info.get("pid")
        if pid in {None, current_pid}:
            continue
        if pid in managed_root_pids:
            continue
        name = str(proc.info.get("name") or "")
        cmdline = _cmdline_text(proc)
        lowered = _normalize_process_text(name, cmdline)
        if not any(keyword in lowered for keyword in AGENT_PROCESS_KEYWORDS):
            continue
        if _has_managed_ancestor(proc, managed_root_pids):
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


def get_managed_status(name: str, script_path: Path, pid_file: Path, *, discover: bool = True) -> ManagedStatus:
    pid = _read_pid_file(pid_file)
    proc = _get_process(pid) if pid else None
    if proc and _process_matches_script(proc, script_path):
        return ManagedStatus(name=name, script_path=script_path, pid_file=pid_file, running=True, pid=proc.pid)

    if not discover:
        return ManagedStatus(name=name, script_path=script_path, pid_file=pid_file, running=False, pid=None)

    discovered = _find_process_by_script(script_path)
    if discovered:
        _write_pid_file(pid_file, discovered.pid)
        return ManagedStatus(name=name, script_path=script_path, pid_file=pid_file, running=True, pid=discovered.pid)

    _clear_pid_file(pid_file)
    return ManagedStatus(name=name, script_path=script_path, pid_file=pid_file, running=False, pid=None)


def get_onebot_runtime_status(*, discover: bool = True) -> ManagedStatus:
    pid = _read_pid_file(ONEBOT_RUNTIME_PID_FILE)
    proc = _get_process(pid) if pid else None
    if proc and any(marker in _cmdline_text(proc).lower() for marker in ONEBOT_RUNTIME_PROCESS_MARKERS):
        return ManagedStatus(name="QQ OneBot Runtime", script_path=Path("OneBot"), pid_file=ONEBOT_RUNTIME_PID_FILE, running=True, pid=proc.pid)

    if not discover:
        return ManagedStatus(name="QQ OneBot Runtime", script_path=Path("OneBot"), pid_file=ONEBOT_RUNTIME_PID_FILE, running=False, pid=None)

    discovered = _find_processes_by_markers(ONEBOT_RUNTIME_PROCESS_MARKERS)
    if discovered:
        primary = discovered[0]
        _write_pid_file(ONEBOT_RUNTIME_PID_FILE, primary.pid)
        return ManagedStatus(name="QQ OneBot Runtime", script_path=Path("OneBot"), pid_file=ONEBOT_RUNTIME_PID_FILE, running=True, pid=primary.pid)

    _clear_pid_file(ONEBOT_RUNTIME_PID_FILE)
    return ManagedStatus(name="QQ OneBot Runtime", script_path=Path("OneBot"), pid_file=ONEBOT_RUNTIME_PID_FILE, running=False, pid=None)


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
    for script_path in (HUB_SCRIPT, BRIDGE_SCRIPT, QQ_BRIDGE_SCRIPT):
        for proc in _find_processes_by_script(script_path):
            for key, value in _read_process_proxy_env(proc.pid).items():
                values.setdefault(key, value)
    return values


def _discover_windows_user_env() -> dict[str, str]:
    if not IS_WINDOWS:
        return {}
    try:
        import winreg

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment")
    except (ImportError, OSError):
        return {}
    values: dict[str, str] = {}
    try:
        index = 0
        while True:
            try:
                name, value, _value_type = winreg.EnumValue(key, index)
            except OSError:
                break
            index += 1
            cleaned_name = str(name or "").strip()
            cleaned_value = str(value or "").strip()
            if cleaned_name and cleaned_value:
                values[cleaned_name] = cleaned_value
    finally:
        winreg.CloseKey(key)
    return values


def _managed_subprocess_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(base_env or os.environ)
    existing_keys = {key.upper() for key in env}
    for key, value in _discover_windows_user_env().items():
        if key.upper() not in existing_keys:
            env[key] = value
            existing_keys.add(key.upper())
    for key, value in _discover_proxy_env().items():
        if not env.get(key, "").strip():
            env[key] = value
    return env


def _split_command(raw_command: str) -> list[str]:
    if IS_WINDOWS:
        return [raw_command]
    return shlex.split(raw_command)


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _resolve_onebot_runtime_command() -> list[str]:
    raw_command = str(os.environ.get(ONEBOT_RUNTIME_COMMAND_ENV) or "").strip()
    if raw_command:
        return _split_command(raw_command)
    for executable_name in ("llonebot", "LLOneBot", "lagrange.onebot", "lagrange-onebot", "napcat"):
        executable = shutil_which(executable_name)
        if executable:
            return [executable]
    installed = find_installed_napcat_launcher()
    if installed is not None:
        ensure_default_onebot_runtime()
        return [str(installed)]
    return []


def _onebot_runtime_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    env = _managed_subprocess_env(base_env)
    env.setdefault("NAPCAT_WEBUI_SECRET_KEY", NAPCAT_WEBUI_TOKEN)
    env.setdefault("NAPCAT_WEBUI_PREFERRED_PORT", "6099")
    _apply_onebot_profile_isolation(env)
    if not env.get(NAPCAT_QUICK_LOGIN_ENV, "").strip():
        quick_login_uin = _detect_napcat_quick_login_uin()
        if quick_login_uin:
            env[NAPCAT_QUICK_LOGIN_ENV] = quick_login_uin
    return env


def _apply_onebot_profile_isolation(env: dict[str, str]) -> None:
    if not IS_WINDOWS:
        return
    if str(env.get(ONEBOT_PROFILE_ISOLATION_ENV, "1")).strip().lower() in {"0", "false", "no", "off"}:
        return
    raw_root = str(env.get(ONEBOT_PROFILE_ROOT_ENV) or "").strip()
    profile_root = Path(raw_root).expanduser() if raw_root else ONEBOT_RUNTIME_DIR / "qq-profile"
    profile_root = profile_root.resolve()
    user_root = profile_root / "User"
    appdata = user_root / "AppData" / "Roaming"
    localappdata = user_root / "AppData" / "Local"
    for path in (appdata, localappdata):
        path.mkdir(parents=True, exist_ok=True)
    env["USERPROFILE"] = str(user_root)
    env["APPDATA"] = str(appdata)
    env["LOCALAPPDATA"] = str(localappdata)


def _detect_napcat_quick_login_uin() -> str:
    config_dir = ONEBOT_RUNTIME_DIR / NAPCAT_RUNTIME_NAME / "config"
    if not config_dir.is_dir():
        return ""
    candidates = []
    for path in config_dir.glob("onebot11_*.json"):
        suffix = path.stem.removeprefix("onebot11_").strip()
        if suffix.isdigit():
            candidates.append(path)
    if not candidates:
        return ""
    latest = max(candidates, key=lambda path: path.stat().st_mtime)
    return latest.stem.removeprefix("onebot11_").strip()


def _query_onebot_api(action: str, *, timeout: float = 0.8, payload: dict | None = None) -> dict:
    request = urllib.request.Request(
        f"{ONEBOT_API_BASE}/{action}",
        data=json.dumps(payload or {}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    return payload if isinstance(payload, dict) else {}


def _compact_login_payload(payload: dict) -> dict:
    safe_keys = ("isLogin", "isOffline", "online", "good", "loginError", "message", "msg", "status", "retcode")
    compact = {key: payload.get(key) for key in safe_keys if key in payload}
    data = payload.get("data")
    if isinstance(data, dict):
        compact["data"] = {key: data.get(key) for key in safe_keys if key in data}
    return compact


def _format_qq_login_probe_error(stage: str, exc: BaseException) -> str:
    message = " ".join(str(exc).split())
    detail = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    return f"{stage}: {detail}"


def _probe_qq_session(user_id: str) -> tuple[bool, dict, str]:
    try:
        payload = _query_onebot_api(
            "get_stranger_info",
            timeout=0.8,
            payload={"user_id": user_id, "no_cache": True},
        )
    except (OSError, RuntimeError, json.JSONDecodeError, urllib.error.URLError) as exc:
        return False, {}, _format_qq_login_probe_error("QQ 主动在线检查失败", exc)

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    status = str(payload.get("status") or "").strip().lower()
    retcode = payload.get("retcode")
    probed_user_id = str(data.get("user_id") or "").strip()
    if status == "ok" and retcode in (None, 0, "0") and probed_user_id == user_id:
        return True, payload, ""
    message = str(payload.get("wording") or payload.get("message") or "").strip()
    detail = message or f"status={status or '-'}, retcode={retcode}"
    return False, payload, f"QQ 主动在线检查失败: {detail}"


def _audit_qq_login_status(
    *,
    source: str,
    logged_in: bool,
    user_id: str,
    nickname: str,
    napcat_status: dict | None = None,
    onebot_status: dict | None = None,
    error: str = "",
) -> None:
    global _QQ_LOGIN_STATUS_AUDIT_LAST_SIGNATURE, _QQ_LOGIN_STATUS_LAST_CHECKED
    checked_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    _QQ_LOGIN_STATUS_LAST_CHECKED = (str(QQ_LOGIN_STATUS_LOG), checked_at)
    signature = (
        source,
        logged_in,
        user_id,
        nickname,
        json.dumps(_compact_login_payload(napcat_status or {}), sort_keys=True, ensure_ascii=False),
        json.dumps(_compact_login_payload(onebot_status or {}), sort_keys=True, ensure_ascii=False),
        error,
    )
    if signature == _QQ_LOGIN_STATUS_AUDIT_LAST_SIGNATURE:
        return
    _QQ_LOGIN_STATUS_AUDIT_LAST_SIGNATURE = signature
    record = {
        "at": checked_at,
        "source": source,
        "logged_in": logged_in,
        "user_id": user_id,
        "nickname": nickname,
        "napcat_status": _compact_login_payload(napcat_status or {}),
        "onebot_status": _compact_login_payload(onebot_status or {}),
    }
    if error:
        record["error"] = error
    try:
        ensure_runtime_dirs()
        with QQ_LOGIN_STATUS_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError:
        pass


def _latest_qq_login_audit() -> tuple[str, str]:
    try:
        lines = QQ_LOGIN_STATUS_LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "", ""
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        checked_at = str(record.get("at") or "")
        if _QQ_LOGIN_STATUS_LAST_CHECKED and _QQ_LOGIN_STATUS_LAST_CHECKED[0] == str(QQ_LOGIN_STATUS_LOG):
            checked_at = _QQ_LOGIN_STATUS_LAST_CHECKED[1]
        detail = _describe_qq_login_audit(record)
        if detail:
            return detail, checked_at
    return "", ""


def _describe_qq_login_audit(record: dict) -> str:
    napcat_status = record.get("napcat_status") if isinstance(record.get("napcat_status"), dict) else {}
    onebot_status = record.get("onebot_status") if isinstance(record.get("onebot_status"), dict) else {}
    error = str(record.get("error") or "").strip()
    login_error = str(napcat_status.get("loginError") or "").strip()
    if login_error:
        return f"NapCat 登录异常：{login_error}"
    if error:
        return f"登录探测异常：{error}"
    if napcat_status.get("isOffline") is True and record.get("logged_in") is True:
        return "NapCat 显示离线，但 OneBot API 仍可用"
    if record.get("logged_in") is False:
        if napcat_status.get("isOffline") is True:
            return "NapCat 显示未登录或已离线"
        data = onebot_status.get("data") if isinstance(onebot_status.get("data"), dict) else {}
        if data.get("online") is False or data.get("good") is False:
            return "OneBot API 显示未在线"
        return "QQ 未登录"
    return "QQ 登录正常"


def get_qq_login_status() -> tuple[bool, str, str]:
    login_error = ""
    try:
        napcat_status = fetch_napcat_login_status(timeout=0.8)
    except (OSError, RuntimeError, json.JSONDecodeError, urllib.error.URLError) as exc:
        napcat_status = {}
        login_error = _format_qq_login_probe_error("NapCat 状态检查失败", exc)
    if napcat_status.get("isLogin") is True:
        try:
            napcat_info = fetch_napcat_login_info(timeout=0.8)
        except (OSError, RuntimeError, json.JSONDecodeError, urllib.error.URLError) as exc:
            napcat_info = {}
            login_error = _format_qq_login_probe_error("NapCat 账号信息检查失败", exc)
        user_id = str(napcat_info.get("uin") or napcat_info.get("user_id") or "").strip()
        nickname = str(napcat_info.get("nick") or napcat_info.get("nickname") or "").strip()
        logged_in = bool(user_id) and napcat_info.get("online") is not False and napcat_status.get("isOffline") is not True
        onebot_probe = {}
        if logged_in:
            logged_in, onebot_probe, probe_error = _probe_qq_session(user_id)
            if probe_error:
                login_error = f"{login_error}；{probe_error}" if login_error else probe_error
        _audit_qq_login_status(
            source="napcat",
            logged_in=logged_in,
            user_id=user_id,
            nickname=nickname,
            napcat_status=napcat_status,
            onebot_status=onebot_probe,
            error=login_error,
        )
        return logged_in, user_id, nickname
    if "isLogin" in napcat_status or napcat_status.get("isOffline") is True or napcat_status.get("loginError"):
        _audit_qq_login_status(source="napcat", logged_in=False, user_id="", nickname="", napcat_status=napcat_status, error=login_error)
        return False, "", ""
    try:
        status_payload = _query_onebot_api("get_status")
        login_payload = _query_onebot_api("get_login_info")
    except (OSError, RuntimeError, json.JSONDecodeError, urllib.error.URLError) as exc:
        onebot_error = _format_qq_login_probe_error("OneBot API 检查失败", exc)
        error = f"{login_error}；{onebot_error}" if login_error else onebot_error
        _audit_qq_login_status(source="error", logged_in=False, user_id="", nickname="", napcat_status=napcat_status, error=error)
        return False, "", ""

    status_data = status_payload.get("data") if isinstance(status_payload.get("data"), dict) else {}
    login_data = login_payload.get("data") if isinstance(login_payload.get("data"), dict) else {}
    user_id = str(login_data.get("user_id") or "").strip()
    nickname = str(login_data.get("nickname") or "").strip()
    online = status_data.get("online")
    good = status_data.get("good")
    logged_in = bool(user_id) and online is True and good is True
    onebot_probe = status_payload
    if logged_in:
        logged_in, onebot_probe, probe_error = _probe_qq_session(user_id)
        if probe_error:
            login_error = f"{login_error}；{probe_error}" if login_error else probe_error
    _audit_qq_login_status(source="onebot", logged_in=logged_in, user_id=user_id, nickname=nickname, napcat_status=napcat_status, onebot_status=onebot_probe, error=login_error)
    return logged_in, user_id, nickname


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
    duplicate_pids: list[int] = []
    if running:
        if len(running) == 1:
            primary = running[0]
            _write_pid_file(pid_file, primary.pid)
            return f"{name} already running (PID {primary.pid})"
        duplicate_pids = [proc.pid for proc in running]
        for proc in running:
            _taskkill(proc.pid)
        _clear_pid_file(pid_file)
        remaining = _wait_for_script_processes_to_exit(script_path)
        if remaining:
            rendered_remaining = ", ".join(str(proc.pid) for proc in remaining)
            rendered_duplicates = ", ".join(str(pid) for pid in duplicate_pids)
            return f"{name} restart blocked after cleaning duplicate PIDs {rendered_duplicates}; still running PIDs {rendered_remaining}"

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
    if duplicate_pids:
        rendered = ", ".join(str(pid) for pid in duplicate_pids)
        return f"{name} restarted after cleaning duplicate PIDs {rendered} (PID {proc.pid})"
    return f"{name} started (PID {proc.pid})"


def start_managed_command(
    name: str,
    command: list[str],
    pid_file: Path,
    stdout_log: Path,
    stderr_log: Path,
    *,
    env: dict[str, str] | None = None,
) -> str:
    if not command:
        return f"{name} unavailable: external runtime command not found"
    running = get_onebot_runtime_status()
    if running.running and running.pid:
        return f"{name} already running (PID {running.pid})"

    ensure_runtime_dirs()
    command_cwd = APP_DIR
    command_path = Path(command[0]) if command else Path()
    if command_path.exists():
        command_cwd = command_path.parent
    use_shell = IS_WINDOWS and len(command) == 1 and command[0].lower().endswith((".bat", ".cmd"))
    needs_console = IS_WINDOWS and len(command) == 1 and command_path.name.lower() == "lagrange.onebot.exe"
    needs_desktop_start = IS_WINDOWS and command_path.name.lower() == "napcatwinbootmain.exe"
    popen_command: str | list[str] = command[0] if use_shell else command
    if needs_desktop_start:
        powershell = shutil_which("powershell") or shutil_which("pwsh") or "powershell"
        arguments = command[1:]
        argument_list = "@(" + ",".join(_powershell_quote(argument) for argument in arguments) + ")"
        ps_command = (
            "$p = Start-Process -FilePath "
            f"{_powershell_quote(command[0])} "
            f"-ArgumentList {argument_list} "
            f"-WorkingDirectory {_powershell_quote(str(command_cwd))} "
            "-PassThru; $p.Id"
        )
        completed = subprocess.run(
            [powershell, "-NoProfile", "-Command", ps_command],
            cwd=str(APP_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_onebot_runtime_env(env),
            creationflags=creationflags(),
            check=False,
        )
        if completed.returncode != 0:
            return f"{name} failed to start: {completed.stderr.strip() or completed.stdout.strip() or completed.returncode}"
        pid_text = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else ""
        try:
            pid = int(pid_text)
        except ValueError:
            return f"{name} failed to start: invalid PID from launcher {pid_text!r}"
        _write_pid_file(pid_file, pid)
        return f"{name} started (PID {pid})"
    if needs_console:
        launcher_path = command_cwd / "chatbridge-start-lagrange.cmd"
        launcher_path.write_text(f"@echo off\r\n\"{command[0]}\"\r\n", encoding="utf-8")
        powershell = shutil_which("powershell") or shutil_which("pwsh") or "powershell"
        ps_command = (
            "$p = Start-Process -FilePath "
            f"{_powershell_quote(str(launcher_path))} "
            f"-WorkingDirectory {_powershell_quote(str(command_cwd))} "
            "-PassThru; $p.Id"
        )
        completed = subprocess.run(
            [powershell, "-NoProfile", "-Command", ps_command],
            cwd=str(APP_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_onebot_runtime_env(env),
            creationflags=creationflags(),
            check=False,
        )
        if completed.returncode != 0:
            return f"{name} failed to start: {completed.stderr.strip() or completed.stdout.strip() or completed.returncode}"
        pid_text = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else ""
        try:
            pid = int(pid_text)
        except ValueError:
            return f"{name} failed to start: invalid PID from launcher {pid_text!r}"
        _write_pid_file(pid_file, pid)
        return f"{name} started (PID {pid})"
    with stdout_log.open("ab") as out_handle, stderr_log.open("ab") as err_handle:
        proc = subprocess.Popen(
            popen_command,
            cwd=str(command_cwd),
            stdout=None if needs_console else out_handle,
            stderr=None if needs_console else err_handle,
            stdin=None if needs_console else subprocess.DEVNULL,
            env=_onebot_runtime_env(env),
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if needs_console else creationflags(),
            start_new_session=not IS_WINDOWS,
            shell=use_shell,
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


def _wait_for_script_processes_to_exit(script_path: Path, *, timeout_seconds: float = 5.0) -> list[object]:
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    remaining = _find_processes_by_script(script_path)
    while remaining and time.monotonic() < deadline:
        time.sleep(0.1)
        remaining = _find_processes_by_script(script_path)
    return remaining


def stop_managed(name: str, script_path: Path, pid_file: Path) -> str:
    running = _find_processes_by_script(script_path)
    if not running:
        _clear_pid_file(pid_file)
        return f"{name} is not running"
    stopped_pids: list[int] = []
    for proc in running:
        stopped_pids.append(proc.pid)
        _taskkill(proc.pid)
    remaining = _wait_for_script_processes_to_exit(script_path)
    _clear_pid_file(pid_file)
    if remaining:
        rendered_remaining = ", ".join(str(proc.pid) for proc in remaining)
        rendered_stopped = ", ".join(str(pid) for pid in stopped_pids)
        return f"{name} stop requested (PIDs {rendered_stopped}); still running PIDs {rendered_remaining}"
    if len(stopped_pids) == 1:
        return f"{name} stopped (PID {stopped_pids[0]})"
    rendered = ", ".join(str(pid) for pid in stopped_pids)
    return f"{name} stopped (PIDs {rendered})"


def start_all(*, env: dict[str, str] | None = None) -> list[str]:
    messages = [stop_qq_bridge()]
    messages.append(stop_onebot_runtime())
    messages.append(start_managed("Hub", HUB_SCRIPT, HUB_PID_FILE, HUB_OUT_LOG, HUB_ERR_LOG, env=env))
    time.sleep(1.5)
    messages.append(start_managed("Bridge", BRIDGE_SCRIPT, BRIDGE_PID_FILE, BRIDGE_OUT_LOG, BRIDGE_ERR_LOG, env=env))
    return messages


def start_bridge(*, env: dict[str, str] | None = None) -> str:
    return start_managed("Bridge", BRIDGE_SCRIPT, BRIDGE_PID_FILE, BRIDGE_OUT_LOG, BRIDGE_ERR_LOG, env=env)


def start_qq_bridge(*, env: dict[str, str] | None = None) -> str:
    return start_managed("QQ Bridge", QQ_BRIDGE_SCRIPT, QQ_BRIDGE_PID_FILE, QQ_BRIDGE_OUT_LOG, QQ_BRIDGE_ERR_LOG, env=env)


def start_onebot_runtime(*, env: dict[str, str] | None = None) -> str:
    command = _resolve_onebot_runtime_command()
    if not command:
        install_result = ensure_default_onebot_runtime()
        if not install_result.ok or install_result.executable_path is None:
            return install_result.message
        command = [str(install_result.executable_path)]
    return start_managed_command("QQ OneBot Runtime", command, ONEBOT_RUNTIME_PID_FILE, ONEBOT_RUNTIME_OUT_LOG, ONEBOT_RUNTIME_ERR_LOG, env=env)


def start_qq_stack(*, env: dict[str, str] | None = None) -> list[str]:
    messages = [stop_bridge()]
    messages.append(start_managed("Hub", HUB_SCRIPT, HUB_PID_FILE, HUB_OUT_LOG, HUB_ERR_LOG, env=env))
    messages.append(start_onebot_runtime(env=env))
    messages.append(start_qq_bridge(env=env))
    return messages


def stop_bridge() -> str:
    return stop_managed("Bridge", BRIDGE_SCRIPT, BRIDGE_PID_FILE)


def stop_qq_bridge() -> str:
    return stop_managed("QQ Bridge", QQ_BRIDGE_SCRIPT, QQ_BRIDGE_PID_FILE)


def stop_onebot_runtime() -> str:
    running_processes = list(
        {
            getattr(proc, "pid", 0): proc
            for proc in [*_find_processes_by_markers(ONEBOT_RUNTIME_PROCESS_MARKERS), *_find_onebot_runtime_port_processes()]
        }.values()
    )
    if not running_processes:
        _clear_pid_file(ONEBOT_RUNTIME_PID_FILE)
        return "QQ OneBot Runtime is not running"
    stopped_pids: list[int] = []
    for proc in running_processes:
        stopped_pids.append(proc.pid)
        _taskkill(proc.pid)
    _clear_pid_file(ONEBOT_RUNTIME_PID_FILE)
    if len(stopped_pids) == 1:
        return f"QQ OneBot Runtime stopped (PID {stopped_pids[0]})"
    return f"QQ OneBot Runtime stopped (PIDs {', '.join(str(pid) for pid in stopped_pids)})"


def restart_bridge() -> list[str]:
    env = _managed_subprocess_env()
    return [stop_bridge(), start_bridge(env=env)]


def restart_hub() -> list[str]:
    env = _managed_subprocess_env()
    return [
        stop_managed("Hub", HUB_SCRIPT, HUB_PID_FILE),
        start_managed("Hub", HUB_SCRIPT, HUB_PID_FILE, HUB_OUT_LOG, HUB_ERR_LOG, env=env),
    ]


def restart_qq_bridge() -> list[str]:
    env = _managed_subprocess_env()
    return [stop_bridge(), stop_qq_bridge(), start_qq_bridge(env=env)]


def restart_qq_stack() -> list[str]:
    env = _managed_subprocess_env()
    messages = [stop_bridge()]
    messages.append(stop_qq_bridge())
    messages.append(stop_managed("Hub", HUB_SCRIPT, HUB_PID_FILE))
    messages.append(start_managed("Hub", HUB_SCRIPT, HUB_PID_FILE, HUB_OUT_LOG, HUB_ERR_LOG, env=env))
    time.sleep(1.5)
    messages.append(start_qq_bridge(env=env))
    return messages


def restart_onebot_runtime() -> list[str]:
    env = _onebot_runtime_env()
    return [stop_bridge(), stop_qq_bridge(), stop_onebot_runtime(), start_onebot_runtime(env=env), start_qq_bridge(env=env)]


def stop_all() -> list[str]:
    messages = [stop_qq_bridge()]
    messages.append(stop_onebot_runtime())
    messages.append(stop_managed("Bridge", BRIDGE_SCRIPT, BRIDGE_PID_FILE))
    messages.append(stop_managed("Hub", HUB_SCRIPT, HUB_PID_FILE))
    return messages


def restart_all() -> list[str]:
    env = _managed_subprocess_env()
    messages = [stop_qq_bridge()]
    messages.append(stop_onebot_runtime())
    messages.append(stop_managed("Bridge", BRIDGE_SCRIPT, BRIDGE_PID_FILE))
    messages.append(stop_managed("Hub", HUB_SCRIPT, HUB_PID_FILE))
    messages.append(start_managed("Hub", HUB_SCRIPT, HUB_PID_FILE, HUB_OUT_LOG, HUB_ERR_LOG, env=env))
    time.sleep(1.5)
    messages.append(start_managed("Bridge", BRIDGE_SCRIPT, BRIDGE_PID_FILE, BRIDGE_OUT_LOG, BRIDGE_ERR_LOG, env=env))
    return messages


def emergency_stop() -> list[str]:
    messages = stop_all()
    if psutil is not None:
        targets: list[int] = []
        for proc in _iter_agent_candidate_processes():
            cmdline = _cmdline_text(proc)
            name = str(proc.info.get("name") or "")
            lowered = _normalize_process_text(name, cmdline)
            if any(keyword in lowered for keyword in AGENT_PROCESS_KEYWORDS):
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
    for proc in _iter_agent_candidate_processes():
        pid = proc.info.get("pid")
        if pid == current_pid:
            continue
        cmdline = _cmdline_text(proc)
        name = str(proc.info.get("name") or "")
        lowered = _normalize_process_text(name, cmdline)
        if not any(keyword in lowered for keyword in AGENT_PROCESS_KEYWORDS):
            continue
        rendered.append(f"PID {pid} :: {cmdline or name}")
    return sorted(rendered)


def read_json(path: Path) -> dict:
    data = load_json(path, {}, expect_type=dict)
    return data if isinstance(data, dict) else {}


def get_runtime_snapshot(
    *,
    include_agent_processes: bool = True,
    include_qq_login_status: bool = True,
    discover_missing_processes: bool = True,
) -> RuntimeSnapshot:
    hub = get_managed_status("Hub", HUB_SCRIPT, HUB_PID_FILE, discover=discover_missing_processes)
    bridge = get_managed_status("Bridge", BRIDGE_SCRIPT, BRIDGE_PID_FILE, discover=discover_missing_processes)
    qq_bridge = get_managed_status("QQ Bridge", QQ_BRIDGE_SCRIPT, QQ_BRIDGE_PID_FILE, discover=discover_missing_processes)
    onebot_runtime = get_onebot_runtime_status(discover=discover_missing_processes)
    qq_logged_in, qq_user_id, qq_nickname = get_qq_login_status() if include_qq_login_status and onebot_runtime.running else (False, "", "")
    qq_login_detail, qq_login_checked_at = _latest_qq_login_audit() if include_qq_login_status else ("", "")
    return RuntimeSnapshot(
        hub_running=hub.running,
        bridge_running=bridge.running,
        qq_bridge_running=qq_bridge.running,
        onebot_runtime_running=onebot_runtime.running,
        hub_pid=hub.pid,
        bridge_pid=bridge.pid,
        qq_bridge_pid=qq_bridge.pid,
        onebot_runtime_pid=onebot_runtime.pid,
        codex_processes=list_codex_processes() if include_agent_processes else [],
        log_dir=str(LOG_DIR),
        qq_logged_in=qq_logged_in,
        qq_user_id=qq_user_id,
        qq_nickname=qq_nickname,
        qq_login_detail=qq_login_detail,
        qq_login_checked_at=qq_login_checked_at,
    )
