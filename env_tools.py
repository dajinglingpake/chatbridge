from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from bridge_config import BridgeConfig
from core.platform_compat import IS_WINDOWS, command_candidates, creationflags, resolve_command, shell_command


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
            creationflags=creationflags(),
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

    if IS_WINDOWS:
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
    else:
        results.append(CheckResult(key="node_manager", label="Node Manager", ok=True, detail="not required on this platform"))

    for key, label, binary in [
        ("node", "Node.js", "node"),
        ("npm", "npm", "npm"),
        ("codex", "Codex CLI", "codex"),
        ("claude", "Claude Code", "claude"),
        ("opencode", "OpenCode CLI", "opencode"),
    ]:
        ok = False
        detail = "not found"
        for candidate in command_candidates(binary):
            ok, detail = _run_capture([candidate, "--version"])
            if ok:
                break
        results.append(CheckResult(key=key, label=label, ok=ok, detail=detail or "not found"))

    psutil_ok = importlib.util.find_spec("psutil") is not None
    results.append(CheckResult(key="psutil", label="psutil", ok=psutil_ok, detail="installed" if psutil_ok else "missing"))

    bridge_config = BridgeConfig.load()
    active_account = bridge_config.get_active_account()
    account_dir = WEIXIN_ACCOUNTS_DIR
    account_ok = active_account.is_usable
    results.append(
        CheckResult(
            key="weixin_account",
            label="Weixin Account Files",
            ok=account_ok,
            detail=f"{active_account.account_id} -> {active_account.account_path}",
        )
    )

    config_files = [
        project_dir / "agent_hub_config.json",
        project_dir / "weixin_bridge_config.json",
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
    commands = [("Install Python deps", "python -m pip install -r requirements.txt")]
    if IS_WINDOWS:
        commands.extend(
            [
                ("Install NVM for Windows", "winget install CoreyButler.NVMforWindows --accept-package-agreements --accept-source-agreements"),
                ("Install Node via NVM", build_nvm_node_command()),
                ("Install Codex CLI", f"{resolve_command('npm')} install -g codex"),
                ("Install Claude Code", f"{resolve_command('npm')} install -g @anthropic-ai/claude-code"),
                ("Install OpenCode CLI", f"{resolve_command('npm')} install -g opencode-ai"),
            ]
        )
    else:
        commands.extend(
            [
                ("Install Node.js", "Use your Linux package manager to install nodejs and npm"),
                ("Install Codex CLI", f"{resolve_command('npm')} install -g codex"),
                ("Install Claude Code", f"{resolve_command('npm')} install -g @anthropic-ai/claude-code"),
                ("Install OpenCode CLI", f"{resolve_command('npm')} install -g opencode-ai"),
            ]
        )
    return commands


def suggested_upgrade_commands() -> list[tuple[str, str]]:
    commands = [
        ("Upgrade pip", "python -m pip install --upgrade pip"),
        ("Upgrade Python deps", "python -m pip install --upgrade -r requirements.txt"),
    ]
    if IS_WINDOWS:
        commands.append(("Upgrade Node via NVM", build_nvm_node_command()))
    commands.extend(
        [
            ("Upgrade Codex CLI", f"{resolve_command('npm')} install -g codex@latest"),
            ("Upgrade Claude Code", f"{resolve_command('npm')} install -g @anthropic-ai/claude-code@latest"),
            ("Upgrade OpenCode CLI", f"{resolve_command('npm')} install -g opencode-ai@latest"),
        ]
    )
    return commands


def run_shell_command(command: str, workdir: Path) -> tuple[int, str]:
    completed = subprocess.run(
        shell_command(command),
        cwd=str(workdir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags(),
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
