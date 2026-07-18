from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import socket
import site
import subprocess
import sys
import time
from pathlib import Path

try:
    import psutil
except ImportError:  # pragma: no cover - optional dependency
    psutil = None

APP_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = APP_DIR / ".runtime"
NATIVE_UI_PIDS_PATH = RUNTIME_DIR / "native-ui.pids"
VENV_DIR = APP_DIR / ".venv"
REQUIREMENTS_PATH = APP_DIR / "requirements.txt"
IMPORT_NAME_OVERRIDES = {
    "Pillow": "PIL",
    "websocket-client": "websocket",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ChatBridge 统一 UI 模式")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--native", action="store_true", help="以本地壳模式启动 NiceGUI")
    return parser.parse_args()


def _venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _is_running_in_project_venv() -> bool:
    expected = VENV_DIR.resolve()
    virtual_env = os.environ.get("VIRTUAL_ENV")
    if virtual_env and Path(virtual_env).resolve() == expected:
        return True
    return Path(sys.prefix).resolve() == expected


def _is_debugger_attached() -> bool:
    return sys.gettrace() is not None or os.environ.get("PYCHARM_HOSTED") == "1"


def _venv_site_packages() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Lib" / "site-packages"
    return VENV_DIR / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"


def _activate_venv_in_process() -> None:
    scripts_dir = _venv_python().parent
    site_packages = _venv_site_packages()
    os.environ["VIRTUAL_ENV"] = str(VENV_DIR)
    os.environ["PATH"] = str(scripts_dir) + os.pathsep + os.environ.get("PATH", "")
    if site_packages.exists():
        site.addsitedir(str(site_packages))
    importlib.invalidate_caches()


def _requirement_import_name(requirement: str) -> str:
    cleaned = requirement.strip()
    for marker in ("==", ">=", "<=", "~=", "!=", ">", "<", "["):
        if marker in cleaned:
            cleaned = cleaned.split(marker, 1)[0]
    return IMPORT_NAME_OVERRIDES.get(cleaned, cleaned.replace("-", "_"))


def _required_dependency_modules() -> list[str]:
    if not REQUIREMENTS_PATH.exists():
        return ["nicegui"]
    modules: list[str] = []
    for raw_line in REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http://", "https://")):
            continue
        module = _requirement_import_name(line)
        if module and module not in modules:
            modules.append(module)
    return modules or ["nicegui"]


def _missing_required_dependency_modules() -> list[str]:
    importlib.invalidate_caches()
    return [module for module in _required_dependency_modules() if importlib.util.find_spec(module) is None]


def _clean_subprocess_env() -> dict[str, str]:
    blocked_exact = {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"}
    cleaned: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in blocked_exact:
            continue
        if "PYCHARM" in upper or "PYDEV" in upper:
            continue
        cleaned[key] = value
    return cleaned


def _python_module_cmd(python_executable: str, module: str, *args: str) -> list[str]:
    return [python_executable, "-I", "-m", module, *args]


@contextlib.contextmanager
def _without_debugger_subprocess_patch():
    patched_module = None
    original_create_process = None
    patched_create_process = None
    if _is_debugger_attached() and os.name == "nt":
        try:
            import _winapi as patched_module
        except ImportError:  # pragma: no cover - CPython on Windows should provide _winapi
            patched_module = None
        if patched_module is not None:
            original_create_process = getattr(patched_module, "original_CreateProcess", None)
            patched_create_process = getattr(patched_module, "CreateProcess", None)
            if original_create_process is not None:
                patched_module.CreateProcess = original_create_process
    try:
        yield
    finally:
        if patched_module is not None and patched_create_process is not None:
            patched_module.CreateProcess = patched_create_process


def _hidden_subprocess_kwargs() -> dict[str, object]:
    if os.name != "nt":
        return {}
    kwargs: dict[str, object] = {}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    kwargs["startupinfo"] = startupinfo
    return kwargs

def _venv_missing_required_dependency_modules(python_executable: str) -> list[str]:
    modules = _required_dependency_modules()
    script = (
        "import importlib.util, json; "
        f"modules = {json.dumps(modules)}; "
        "print(json.dumps([module for module in modules if importlib.util.find_spec(module) is None]))"
    )
    with _without_debugger_subprocess_patch():
        completed = subprocess.run(
            [python_executable, "-I", "-c", script],
            cwd=str(APP_DIR),
            capture_output=True,
            text=True,
            env=_clean_subprocess_env(),
            check=False,
            **_hidden_subprocess_kwargs(),
        )
    if completed.returncode != 0:
        return modules
    try:
        payload = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError:
        return modules
    return [str(item) for item in payload]


def _run_command(argv: list[str]) -> None:
    with _without_debugger_subprocess_patch():
        completed = subprocess.run(
            argv,
            cwd=str(APP_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_clean_subprocess_env(),
            check=False,
            **_hidden_subprocess_kwargs(),
        )
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    if completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode,
            argv,
            output=completed.stdout,
            stderr=completed.stderr,
        )


def _ensure_venv_pip(python_executable: str) -> None:
    with _without_debugger_subprocess_patch():
        pip_check = subprocess.run(
            _python_module_cmd(python_executable, "pip", "--version"),
            cwd=str(APP_DIR),
            capture_output=True,
            text=True,
            env=_clean_subprocess_env(),
            check=False,
            **_hidden_subprocess_kwargs(),
        )
    if pip_check.returncode == 0:
        return

    print("[chatbridge] pip missing in local virtual environment, bootstrapping with ensurepip", file=sys.stderr)
    _run_command(_python_module_cmd(python_executable, "ensurepip", "--upgrade", "--default-pip"))
    importlib.invalidate_caches()


def _ensure_local_venv() -> Path:
    venv_python = _venv_python()
    if venv_python.exists():
        return venv_python

    print(f"[chatbridge] Creating local virtual environment: {VENV_DIR}", file=sys.stderr)
    try:
        _run_command(_python_module_cmd(sys.executable, "venv", str(VENV_DIR)))
    except subprocess.CalledProcessError:
        if not venv_python.exists():
            raise
        print(
            "[chatbridge] venv creation reported an error after creating the interpreter; attempting to repair pip in-place",
            file=sys.stderr,
        )
    return venv_python


def ensure_ui_dependencies(launcher_path: Path | None = None) -> None:
    entry_script = str((launcher_path or APP_DIR / "ui_main.py").resolve())
    venv_python = _ensure_local_venv()
    installer_python = str(venv_python)

    if not _is_running_in_project_venv() and _is_debugger_attached():
        _activate_venv_in_process()

    missing_modules = (
        _missing_required_dependency_modules()
        if _is_running_in_project_venv()
        else _venv_missing_required_dependency_modules(installer_python)
    )
    if missing_modules:
        _ensure_venv_pip(installer_python)
        print(f"[chatbridge] Installing Python dependencies from {REQUIREMENTS_PATH.name}", file=sys.stderr)
        _run_command(_python_module_cmd(installer_python, "pip", "install", "--upgrade", "pip"))
        _run_command(_python_module_cmd(installer_python, "pip", "install", "-r", str(REQUIREMENTS_PATH)))
        importlib.invalidate_caches()
        if not _is_running_in_project_venv():
            if _is_debugger_attached():
                _activate_venv_in_process()
            else:
                os.execv(installer_python, [installer_python, entry_script, *sys.argv[1:]])
        missing_modules = _missing_required_dependency_modules()
        venv_missing_modules = _venv_missing_required_dependency_modules(installer_python)
        if missing_modules and venv_missing_modules:
            missing_text = ", ".join(venv_missing_modules)
            raise RuntimeError(f"Python dependencies are still unavailable after installing requirements: {missing_text}")
        return

    if not _is_running_in_project_venv():
        if _is_debugger_attached():
            _activate_venv_in_process()
        else:
            os.execv(installer_python, [installer_python, entry_script, *sys.argv[1:]])


def _detect_local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def _print_access_urls(host: str, port: int, native: bool) -> None:
    if native:
        print(f"[chatbridge] Native UI mode | port={port}", file=sys.stderr)
        return

    local_url = f"http://127.0.0.1:{port}"
    if host in {"0.0.0.0", "::"}:
        lan_url = f"http://{_detect_local_ip()}:{port}"
        print(f"[chatbridge] Local URL:   {local_url}", file=sys.stderr)
        print(f"[chatbridge] Remote URL:  {lan_url}", file=sys.stderr)
        return

    bind_url = f"http://{host}:{port}"
    print(f"[chatbridge] Access URL:  {bind_url}", file=sys.stderr)
    if host not in {"127.0.0.1", "localhost"}:
        print(f"[chatbridge] Local URL:   {local_url}", file=sys.stderr)

def _native_ui_script_paths(launcher_path: Path | None) -> set[str]:
    paths = {APP_DIR / "main.py", APP_DIR / "ui_main.py"}
    if launcher_path is not None:
        paths.add(launcher_path)
    resolved: set[str] = set()
    for path in paths:
        text = str(path.resolve()).lower()
        resolved.add(text)
        resolved.add(text.replace("\\", "/"))
    return resolved

def _process_cmdline(proc: object) -> list[str]:
    try:
        return [str(item) for item in proc.cmdline()]
    except (AttributeError, OSError):
        return []
    except Exception as exc:  # pragma: no cover - psutil.Error depends on optional psutil
        if psutil is not None and isinstance(exc, psutil.Error):
            return []
        raise

def _process_name(proc: object) -> str:
    try:
        info = getattr(proc, "info", {})
        name = info.get("name") if isinstance(info, dict) else None
        if name:
            return str(name)
    except (AttributeError, OSError):
        pass
    try:
        return str(proc.name())
    except (AttributeError, OSError):
        return ""
    except Exception as exc:  # pragma: no cover - psutil.Error depends on optional psutil
        if psutil is not None and isinstance(exc, psutil.Error):
            return ""
        raise

def _process_parent_pid(proc: object) -> int | None:
    try:
        return int(proc.ppid())
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    except Exception as exc:  # pragma: no cover - psutil.Error depends on optional psutil
        if psutil is not None and isinstance(exc, psutil.Error):
            return None
        raise

def _process_cwd(proc: object) -> str:
    try:
        info = getattr(proc, "info", {})
        cwd = info.get("cwd") if isinstance(info, dict) else None
        if cwd:
            return str(cwd)
    except (AttributeError, OSError):
        pass
    try:
        return str(proc.cwd())
    except (AttributeError, OSError):
        return ""
    except Exception as exc:  # pragma: no cover - psutil.Error depends on optional psutil
        if psutil is not None and isinstance(exc, psutil.Error):
            return ""
        raise

def _current_process_family_pids() -> set[int]:
    current_pid = os.getpid()
    family = {current_pid}
    if psutil is None:
        return family
    try:
        family.update(parent.pid for parent in psutil.Process(current_pid).parents())
    except (psutil.Error, OSError):
        pass
    return family

def _read_native_ui_pid_file() -> list[int]:
    try:
        text = NATIVE_UI_PIDS_PATH.read_text(encoding="utf-8")
    except OSError:
        return []
    pids: list[int] = []
    for item in text.replace(",", "\n").splitlines():
        try:
            pid = int(item.strip())
        except ValueError:
            continue
        if pid > 0:
            pids.append(pid)
    return pids

def _write_native_ui_pid_file(pids: list[int]) -> None:
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        NATIVE_UI_PIDS_PATH.write_text("\n".join(str(pid) for pid in pids), encoding="utf-8")
    except OSError:
        pass

def _is_project_dir(value: str) -> bool:
    if not value:
        return False
    try:
        return Path(value).resolve() == APP_DIR
    except OSError:
        return False

def _is_native_ui_script_arg(value: str) -> bool:
    normalized = value.strip("\"'").replace("\\", "/").lower()
    return normalized in {"main.py", "./main.py", "ui_main.py", "./ui_main.py"} or normalized.endswith(("/main.py", "/ui_main.py"))

def _cmdline_port_matches(cmdline: list[str], port: int) -> bool:
    expected = str(int(port))
    for index, item in enumerate(cmdline):
        cleaned = item.strip("\"'")
        if cleaned == "--port" and index + 1 < len(cmdline):
            return cmdline[index + 1].strip("\"'") == expected
        if cleaned.startswith("--port="):
            return cleaned.split("=", 1)[1] == expected
    return int(port) == 8765

def _is_ui_server_process(proc: object, script_paths: set[str], port: int) -> bool:
    cmdline = _process_cmdline(proc)
    if "--native" in cmdline:
        return False
    if not _cmdline_port_matches(cmdline, port):
        return False
    joined = " ".join(cmdline).lower()
    normalized = joined.replace("\\", "/")
    if any(path in joined or path in normalized for path in script_paths):
        return True
    return _is_project_dir(_process_cwd(proc)) and any(_is_native_ui_script_arg(item) for item in cmdline)

def _native_window_parent_pid(proc: object) -> int | None:
    joined = " ".join(_process_cmdline(proc))
    marker = "spawn_main(parent_pid="
    if "--multiprocessing-fork" not in joined or marker not in joined:
        return None
    start = joined.find(marker) + len(marker)
    end = joined.find(",", start)
    if end < 0:
        end = joined.find(")", start)
    try:
        return int(joined[start:end])
    except ValueError:
        return None

def _is_native_ui_process(proc: object, script_paths: set[str]) -> bool:
    cmdline = _process_cmdline(proc)
    if "--native" not in cmdline:
        return False
    joined = " ".join(cmdline).lower()
    normalized = joined.replace("\\", "/")
    if any(path in joined or path in normalized for path in script_paths):
        return True
    return _is_project_dir(_process_cwd(proc)) and any(_is_native_ui_script_arg(item) for item in cmdline)

def _is_native_webview_process(proc: object) -> bool:
    if _process_name(proc).lower() != "msedgewebview2.exe":
        return False
    return "--webview-exe-name=python" in " ".join(_process_cmdline(proc)).lower()

def _window_pids_by_title(title: str) -> list[int]:
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return []
    user32 = ctypes.windll.user32
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        if buffer.value == title:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                found.append(int(pid.value))
        return True

    user32.EnumWindows(enum_proc, 0)
    return found

def _native_webview_descendant_pids(root_pids: set[int], processes: list[object]) -> list[int]:
    children_by_parent: dict[int, list[object]] = {}
    for proc in processes:
        parent_pid = _process_parent_pid(proc)
        if parent_pid is not None:
            children_by_parent.setdefault(parent_pid, []).append(proc)
    found: list[int] = []
    stack = list(root_pids)
    while stack:
        parent_pid = stack.pop()
        for child in children_by_parent.get(parent_pid, []):
            pid = int(getattr(child, "pid", 0) or getattr(child, "info", {}).get("pid") or 0)
            if not pid:
                continue
            stack.append(pid)
            if _is_native_webview_process(child):
                found.append(pid)
    return found

def _native_ui_process_from_pid(pid: int, script_paths: set[str]) -> object | None:
    if psutil is None:
        return None
    try:
        proc = psutil.Process(pid)
    except (psutil.Error, OSError):
        return None
    return proc if _is_native_ui_process(proc, script_paths) else None

def _current_native_ui_pids(script_paths: set[str]) -> list[int]:
    if psutil is None:
        return [os.getpid()]
    try:
        current = psutil.Process(os.getpid())
        processes = [current, *current.parents()]
    except (psutil.Error, OSError):
        return [os.getpid()]
    pids: list[int] = []
    for proc in processes:
        pid = int(getattr(proc, "pid", 0) or 0)
        if pid and _is_native_ui_process(proc, script_paths):
            pids.append(pid)
    return pids or [os.getpid()]

def _terminate_process_only(pid: int) -> None:
    if psutil is not None:
        try:
            psutil.Process(pid).kill()
        except (psutil.Error, OSError):
            pass
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            **_hidden_subprocess_kwargs(),
        )
        return

def _close_previous_ui_server_instances(port: int, launcher_path: Path | None = None) -> list[int]:
    if psutil is None:
        return []
    script_paths = _native_ui_script_paths(launcher_path)
    current_family = _current_process_family_pids()
    stopped: list[int] = []
    stopped_set: set[int] = set()
    for proc in psutil.process_iter(["pid", "ppid", "name", "cmdline", "cwd"]):
        pid = int(getattr(proc, "pid", 0) or proc.info.get("pid") or 0)
        if not pid or pid in current_family or pid in stopped_set:
            continue
        if not _is_ui_server_process(proc, script_paths, port):
            continue
        _terminate_process_only(pid)
        stopped.append(pid)
        stopped_set.add(pid)
    return stopped

def _close_previous_native_ui_instances(launcher_path: Path | None = None) -> list[int]:
    if psutil is None:
        return []
    script_paths = _native_ui_script_paths(launcher_path)
    current_family = _current_process_family_pids()
    processes = list(psutil.process_iter(["pid", "ppid", "name", "cmdline", "cwd"]))
    old_ui_pids: list[int] = []
    old_ui_pid_set: set[int] = set()
    for pid in _read_native_ui_pid_file():
        if pid in current_family or pid in old_ui_pid_set:
            continue
        if _native_ui_process_from_pid(pid, script_paths) is not None:
            old_ui_pids.append(pid)
            old_ui_pid_set.add(pid)
    matches = [
        proc
        for proc in processes
        if int(proc.info.get("pid") or getattr(proc, "pid", 0) or 0) not in current_family
        and _is_native_ui_process(proc, script_paths)
    ]
    for proc in matches:
        pid = int(getattr(proc, "pid", 0) or proc.info.get("pid") or 0)
        if pid and pid not in old_ui_pid_set:
            old_ui_pids.append(pid)
            old_ui_pid_set.add(pid)
    window_pids = {
        int(getattr(proc, "pid", 0) or getattr(proc, "info", {}).get("pid") or 0)
        for proc in processes
        if (_native_window_parent_pid(proc) in old_ui_pid_set)
        and int(getattr(proc, "pid", 0) or getattr(proc, "info", {}).get("pid") or 0) not in current_family
    }
    window_pids.update(pid for pid in _window_pids_by_title("ChatBridge UI") if pid not in current_family)
    webview_pids = set(_native_webview_descendant_pids(window_pids, processes))
    stopped: list[int] = []
    stopped_set: set[int] = set()
    for pid in [*webview_pids, *window_pids, *old_ui_pids]:
        if pid and pid not in stopped_set:
            _terminate_process_only(pid)
            stopped.append(pid)
            stopped_set.add(pid)
    if old_ui_pids or window_pids:
        time.sleep(0.8)
        processes = list(psutil.process_iter(["pid", "ppid", "name", "cmdline", "cwd"]))
        late_window_pids = {pid for pid in _window_pids_by_title("ChatBridge UI") if pid not in current_family and pid not in stopped_set}
        late_webview_pids = set(_native_webview_descendant_pids(late_window_pids, processes))
        for pid in [*late_webview_pids, *late_window_pids]:
            if pid and pid not in stopped_set:
                _terminate_process_only(pid)
                stopped.append(pid)
                stopped_set.add(pid)
    return stopped

def run_ui_entry(
    host: str = "0.0.0.0",
    port: int = 8765,
    native: bool = False,
    launcher_path: Path | None = None,
) -> None:
    ensure_ui_dependencies(launcher_path=launcher_path)
    stopped_pids = _close_previous_native_ui_instances(launcher_path)
    if stopped_pids:
        print(f"[chatbridge] Closed previous Native UI PIDs: {', '.join(str(pid) for pid in stopped_pids)}", file=sys.stderr)
    if native:
        _write_native_ui_pid_file(_current_native_ui_pids(_native_ui_script_paths(launcher_path)))
    stopped_server_pids = _close_previous_ui_server_instances(port, launcher_path)
    if stopped_server_pids:
        print(f"[chatbridge] Closed previous UI server PIDs: {', '.join(str(pid) for pid in stopped_server_pids)}", file=sys.stderr)
    from ui.app import run_ui

    _print_access_urls(host, port, native)
    run_ui(host=host, port=port, native=native)


def main() -> int:
    args = parse_args()
    run_ui_entry(host=args.host, port=args.port, native=args.native)
    return 0


if __name__ == "__main__":
    main()
