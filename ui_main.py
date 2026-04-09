from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
VENV_DIR = APP_DIR / ".venv"
REQUIREMENTS_PATH = APP_DIR / "requirements.txt"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ChatBridge 统一 UI 模式")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--native", action="store_true", help="以本地壳模式启动 NiceGUI")
    return parser.parse_args()


def _venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _is_running_in_project_venv() -> bool:
    return Path(sys.executable).resolve() == _venv_python().resolve()


def _has_ui_dependency() -> bool:
    return importlib.util.find_spec("nicegui") is not None


def _run_command(argv: list[str]) -> None:
    subprocess.run(argv, cwd=str(APP_DIR), check=True)


def ensure_ui_dependencies() -> None:
    venv_python = _venv_python()
    if venv_python.exists() and not _is_running_in_project_venv():
        os.execv(str(venv_python), [str(venv_python), str(APP_DIR / "ui_main.py"), *sys.argv[1:]])

    if _has_ui_dependency():
        return

    if not venv_python.exists():
        print(f"[chatbridge] Creating local virtual environment: {VENV_DIR}", file=sys.stderr)
        _run_command([sys.executable, "-m", "venv", str(VENV_DIR)])

    installer_python = str(venv_python)
    print(f"[chatbridge] Installing Python dependencies from {REQUIREMENTS_PATH.name}", file=sys.stderr)
    _run_command([installer_python, "-m", "pip", "install", "--upgrade", "pip"])
    _run_command([installer_python, "-m", "pip", "install", "-r", str(REQUIREMENTS_PATH)])

    os.execv(installer_python, [installer_python, str(APP_DIR / "ui_main.py"), *sys.argv[1:]])


def run_ui_entry(host: str = "127.0.0.1", port: int = 8765, native: bool = False) -> None:
    ensure_ui_dependencies()
    from ui.app import run_ui

    run_ui(host=host, port=port, native=native)


def main() -> int:
    args = parse_args()
    run_ui_entry(host=args.host, port=args.port, native=args.native)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
