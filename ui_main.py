from __future__ import annotations

import argparse
import contextlib
import importlib.util
import os
import socket
import site
import subprocess
import sys
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
VENV_DIR = APP_DIR / ".venv"
REQUIREMENTS_PATH = APP_DIR / "requirements.txt"


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


def _has_ui_dependency() -> bool:
    importlib.invalidate_caches()
    return importlib.util.find_spec("nicegui") is not None


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


def _venv_has_ui_dependency(python_executable: str) -> bool:
    with _without_debugger_subprocess_patch():
        completed = subprocess.run(
            [python_executable, "-I", "-c", "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('nicegui') else 1)"],
            cwd=str(APP_DIR),
            capture_output=True,
            text=True,
            env=_clean_subprocess_env(),
            check=False,
        )
    return completed.returncode == 0


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

    if not _has_ui_dependency():
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
        if not _has_ui_dependency() and not _venv_has_ui_dependency(installer_python):
            raise RuntimeError("nicegui is still unavailable after installing requirements into the local virtual environment")
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


def run_ui_entry(
    host: str = "0.0.0.0",
    port: int = 8765,
    native: bool = False,
    launcher_path: Path | None = None,
) -> None:
    ensure_ui_dependencies(launcher_path=launcher_path)
    from ui.app import run_ui

    _print_access_urls(host, port, native)
    run_ui(host=host, port=port, native=native)


def main() -> int:
    args = parse_args()
    run_ui_entry(host=args.host, port=args.port, native=args.native)
    return 0


if __name__ == "__main__":
    main()
