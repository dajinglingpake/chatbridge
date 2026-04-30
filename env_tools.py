from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from bridge_config import BridgeConfig
from core.platform_compat import IS_WINDOWS, command_candidates, creationflags, resolve_command, shell_command


DEFAULT_NODE_VERSION = "24.14.1"
WEIXIN_ACCOUNTS_DIR = Path(__file__).resolve().parent / "accounts"
REQUIREMENTS_PATH = Path(__file__).resolve().parent / "requirements.txt"
IMPORT_NAME_OVERRIDES = {
    "Pillow": "PIL",
}


@dataclass
class CheckResult:
    key: str
    label: str
    ok: bool
    detail: str


FULL_CHECK_SEQUENCE = [
    "python",
    "node_runtime",
    "agent_clis",
    "psutil",
    "weixin_account",
    "project_files",
]

FULL_CHECK_STEP_LABELS = {
    "python": "Python",
    "node_runtime": "Node 环境",
    "agent_clis": "Agent CLI",
    "psutil": "Python 依赖",
    "weixin_account": "微信账号文件",
    "project_files": "项目文件",
}


def get_full_check_sequence() -> list[str]:
    return list(FULL_CHECK_SEQUENCE)


def get_full_check_step_label(step_key: str) -> str:
    return FULL_CHECK_STEP_LABELS.get(step_key, step_key)


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


def _project_files_check(project_dir: Path) -> CheckResult:
    config_files = [
        project_dir / "config" / "agent_hub.json",
        project_dir / "config" / "weixin_bridge.json",
        project_dir / "main.py",
    ]
    return CheckResult(
        key="project_files",
        label="Project Files",
        ok=all(path.exists() for path in config_files),
        detail="; ".join(path.name for path in config_files),
    )


def _requirement_import_name(requirement: str) -> str:
    cleaned = requirement.strip()
    for marker in ("==", ">=", "<=", "~=", "!=", ">", "<", "["):
        if marker in cleaned:
            cleaned = cleaned.split(marker, 1)[0]
    return IMPORT_NAME_OVERRIDES.get(cleaned, cleaned.replace("-", "_"))


def _required_dependency_modules() -> list[str]:
    if not REQUIREMENTS_PATH.exists():
        return ["psutil"]
    modules: list[str] = []
    for raw_line in REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http://", "https://")):
            continue
        module = _requirement_import_name(line)
        if module and module not in modules:
            modules.append(module)
    return modules or ["psutil"]


def _python_dependencies_check() -> CheckResult:
    modules = _required_dependency_modules()
    missing = [module for module in modules if importlib.util.find_spec(module) is None]
    if missing:
        return CheckResult(
            key="psutil",
            label="Python Dependencies",
            ok=False,
            detail=f"missing: {', '.join(missing)}",
        )
    return CheckResult(
        key="psutil",
        label="Python Dependencies",
        ok=True,
        detail=f"installed: {', '.join(modules)}",
    )


def _weixin_account_check(config: BridgeConfig) -> CheckResult:
    active_account = config.get_active_account()
    return CheckResult(
        key="weixin_account",
        label="Weixin Account Files",
        ok=active_account.is_usable,
        detail=f"{active_account.account_id} -> {active_account.account_path}",
    )


def _binary_check(key: str, label: str, binary: str) -> CheckResult:
    ok = False
    detail = "not found"
    for candidate in command_candidates(binary):
        ok, detail = _run_capture([candidate, "--version"])
        if ok:
            break
    return CheckResult(key=key, label=label, ok=ok, detail=detail or "not found")


def collect_check_step(step_key: str, project_dir: Path, config: BridgeConfig | None = None) -> list[CheckResult]:
    bridge_config = config or BridgeConfig.load()
    if step_key == "python":
        py_ok = sys.version_info >= (3, 11)
        return [CheckResult(key="python", label="Python", ok=py_ok, detail=sys.version.split()[0])]
    if step_key == "node_runtime":
        results: list[CheckResult] = []
        if IS_WINDOWS:
            winget_exe = find_winget_exe()
            results.append(CheckResult(key="winget", label="winget", ok=winget_exe is not None, detail=winget_exe or "not found"))
            nvm_exe = find_nvm_exe()
            if nvm_exe:
                ok, detail = _run_capture([nvm_exe, "version"])
                results.append(CheckResult(key="nvm", label="NVM for Windows", ok=ok, detail=detail or nvm_exe))
            else:
                results.append(CheckResult(key="nvm", label="NVM for Windows", ok=False, detail="not found"))
        results.append(_binary_check("node", "Node.js", "node"))
        results.append(_binary_check("npm", "npm", "npm"))
        return results
    if step_key == "agent_clis":
        binary_specs = [
            ("codex", "Codex CLI", "codex"),
            ("claude", "Claude Code", "claude"),
            ("opencode", "OpenCode CLI", "opencode"),
        ]
        with ThreadPoolExecutor(max_workers=len(binary_specs)) as executor:
            return list(executor.map(lambda spec: _binary_check(*spec), binary_specs))
    if step_key == "psutil":
        return [_python_dependencies_check()]
    if step_key == "weixin_account":
        return [_weixin_account_check(bridge_config)]
    if step_key == "project_files":
        return [_project_files_check(project_dir)]
    raise ValueError(f"unknown check step: {step_key}")


def collect_lightweight_checks(project_dir: Path, config: BridgeConfig | None = None) -> list[CheckResult]:
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

    results.append(_python_dependencies_check())

    bridge_config = config or BridgeConfig.load()
    results.append(_weixin_account_check(bridge_config))
    results.append(_project_files_check(project_dir))

    return results


def collect_checks(project_dir: Path) -> list[CheckResult]:
    bridge_config = BridgeConfig.load()
    sequence = get_full_check_sequence()
    ordered_results: list[CheckResult] = []
    for step_key in sequence:
        ordered_results.extend(collect_check_step(step_key, project_dir, bridge_config))
    return ordered_results


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
