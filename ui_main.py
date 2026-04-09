from __future__ import annotations

import argparse
import importlib.util
import os
import socket
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


def _has_ui_dependency() -> bool:
    return importlib.util.find_spec("nicegui") is not None


def _run_command(argv: list[str]) -> None:
    completed = subprocess.run(
        argv,
        cwd=str(APP_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
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
    pip_check = subprocess.run(
        [python_executable, "-m", "pip", "--version"],
        cwd=str(APP_DIR),
        capture_output=True,
        text=True,
        check=False,
    )
    if pip_check.returncode == 0:
        return

    print("[chatbridge] pip missing in local virtual environment, bootstrapping with ensurepip", file=sys.stderr)
    _run_command([python_executable, "-m", "ensurepip", "--upgrade", "--default-pip"])


def _ensure_local_venv() -> Path:
    venv_python = _venv_python()
    if venv_python.exists():
        return venv_python

    print(f"[chatbridge] Creating local virtual environment: {VENV_DIR}", file=sys.stderr)
    try:
        _run_command([sys.executable, "-m", "venv", str(VENV_DIR)])
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
    venv_python = _venv_python()
    if venv_python.exists() and not _is_running_in_project_venv():
        os.execv(str(venv_python), [str(venv_python), entry_script, *sys.argv[1:]])

    if _has_ui_dependency():
        return

    venv_python = _ensure_local_venv()

    installer_python = str(venv_python)
    _ensure_venv_pip(installer_python)
    print(f"[chatbridge] Installing Python dependencies from {REQUIREMENTS_PATH.name}", file=sys.stderr)
    _run_command([installer_python, "-m", "pip", "install", "--upgrade", "pip"])
    _run_command([installer_python, "-m", "pip", "install", "-r", str(REQUIREMENTS_PATH)])

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
