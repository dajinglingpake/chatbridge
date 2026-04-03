from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DEFAULT_NODE_VERSION = "24.14.1"
WEIXIN_ACCOUNTS_DIR = Path(__file__).resolve().parent / "accounts"


@dataclass
class CheckResult:
    key: str
    label: str
    ok: bool
    detail: str


def _run_capture(command: list[str]) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
    except OSError as exc:
        return False, str(exc)
    output = (completed.stdout or completed.stderr or "").strip()
    return completed.returncode == 0, output


def find_nvm_exe() -> str | None:
    direct = shutil.which("nvm")
    if direct:
        return direct

    candidates = [
        Path.home() / "scoop" / "apps" / "nvm" / "current" / "nvm.exe",
        Path("C:/Program Files/nvm/nvm.exe"),
        Path("C:/ProgramData/chocolatey/bin/nvm.exe"),
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    return None


def find_winget_exe() -> str | None:
    direct = shutil.which("winget")
    if direct:
        return direct

    candidate = Path.home() / "AppData/Local/Microsoft/WindowsApps/winget.exe"
    if candidate.exists():
        return str(candidate)
    return None


def build_nvm_node_command(version: str = DEFAULT_NODE_VERSION) -> str:
    nvm_exe = find_nvm_exe()
    if nvm_exe:
        quoted = f"\"{nvm_exe}\""
        return f"{quoted} install {version} && {quoted} use {version}"
    return f"nvm install {version} && nvm use {version}"


def collect_checks(project_dir: Path) -> list[CheckResult]:
    results: list[CheckResult] = []

    py_ok = sys.version_info >= (3, 11)
    results.append(
        CheckResult(
            key="python",
            label="Python",
            ok=py_ok,
            detail=sys.version.split()[0],
        )
    )

    winget_exe = find_winget_exe()
    results.append(
        CheckResult(
            key="winget",
            label="winget",
            ok=winget_exe is not None,
            detail=winget_exe or "not found",
        )
    )

    nvm_exe = find_nvm_exe()
    if nvm_exe:
        ok, detail = _run_capture([nvm_exe, "version"])
        results.append(CheckResult(key="nvm", label="NVM for Windows", ok=ok, detail=detail or nvm_exe))
    else:
        results.append(CheckResult(key="nvm", label="NVM for Windows", ok=False, detail="not found"))

    for key, label, cmd in [
        ("node", "Node.js", ["node", "--version"]),
        ("npm", "npm", ["npm.cmd", "--version"]),
        ("codex", "Codex CLI", ["codex.cmd", "--version"]),
        ("opencode", "OpenCode CLI", ["opencode.cmd", "--version"]),
    ]:
        ok, detail = _run_capture(cmd)
        results.append(CheckResult(key=key, label=label, ok=ok, detail=detail or "not found"))

    pyside6_ok = importlib.util.find_spec("PySide6") is not None
    results.append(CheckResult(key="pyside6", label="PySide6", ok=pyside6_ok, detail="installed" if pyside6_ok else "missing"))

    psutil_ok = importlib.util.find_spec("psutil") is not None
    results.append(CheckResult(key="psutil", label="psutil", ok=psutil_ok, detail="installed" if psutil_ok else "missing"))

    account_dir = WEIXIN_ACCOUNTS_DIR
    account_files = list(account_dir.glob("*.json")) if account_dir.exists() else []
    account_ok = any(not path.name.endswith(".sync.json") and not path.name.endswith(".context-tokens.json") for path in account_files)
    results.append(
        CheckResult(
            key="weixin_account",
            label="Weixin Account Files",
            ok=account_ok,
            detail=str(account_dir),
        )
    )

    config_files = [
        project_dir / "multi_codex_hub_config.json",
        project_dir / "weixin_hub_bridge_config.json",
        project_dir / "main.py",
    ]
    results.append(
        CheckResult(
            key="project_files",
            label="Project Files",
            ok=all(path.exists() for path in config_files),
            detail="; ".join(path.name for path in config_files),
        )
    )

    return results


def suggested_install_commands() -> list[tuple[str, str]]:
    return [
        ("Install Desktop deps", "python -m pip install PySide6 psutil"),
        ("Install NVM for Windows", "winget install CoreyButler.NVMforWindows --accept-package-agreements --accept-source-agreements"),
        ("Install Node via NVM", build_nvm_node_command()),
        ("Install Codex CLI", "npm.cmd install -g codex"),
        ("Install OpenCode CLI", "npm.cmd install -g opencode-ai"),
    ]


def suggested_upgrade_commands() -> list[tuple[str, str]]:
    return [
        ("Upgrade pip", "python -m pip install --upgrade pip"),
        ("Upgrade Desktop deps", "python -m pip install --upgrade PySide6 psutil"),
        ("Upgrade Node via NVM", build_nvm_node_command()),
        ("Upgrade Codex CLI", "npm.cmd install -g codex@latest"),
        ("Upgrade OpenCode CLI", "npm.cmd install -g opencode-ai@latest"),
    ]


def run_shell_command(command: str, workdir: Path) -> tuple[int, str]:
    completed = subprocess.run(
        ["cmd", "/c", command],
        cwd=str(workdir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
        check=False,
    )
    output = "\n".join(part for part in [completed.stdout.strip(), completed.stderr.strip()] if part)
    return completed.returncode, output


def export_check_report(project_dir: Path) -> str:
    payload = {
        "python": sys.version.split()[0],
        "checks": [result.__dict__ for result in collect_checks(project_dir)],
        "commands": [{"label": label, "command": command} for label, command in suggested_install_commands()],
        "upgrade_commands": [{"label": label, "command": command} for label, command in suggested_upgrade_commands()],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
