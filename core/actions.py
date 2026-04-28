from __future__ import annotations

from dataclasses import dataclass

from env_tools import build_nvm_node_command
from core.platform_compat import IS_WINDOWS, resolve_command
from core.state_models import CheckSnapshot


def is_missing(checks: dict[str, CheckSnapshot], key: str) -> bool:
    item = checks.get(key)
    return bool(item and not item.ok)


@dataclass
class RepairCommand:
    label: str
    command: str
    runnable: bool


def is_runnable_command(command: str) -> bool:
    stripped = command.strip()
    if not stripped:
        return False
    if stripped.startswith("请先") or stripped.lower().startswith("use your "):
        return False
    return True


def build_repair_commands(checks: dict[str, CheckSnapshot], translate=None) -> list[tuple[str, str]]:
    commands: list[tuple[str, str]] = []
    npm_command = resolve_command("npm")
    will_have_node = not (is_missing(checks, "node") or is_missing(checks, "npm"))

    def label(key: str, fallback: str) -> str:
        if translate is None:
            return fallback
        return translate(key)

    if is_missing(checks, "psutil"):
        commands.append((label("ui.quickstart.step.desktop", "Python 依赖"), "python -m pip install -r requirements.txt"))

    if IS_WINDOWS and is_missing(checks, "nvm") and not is_missing(checks, "winget"):
        commands.append(("NVM for Windows", "winget install CoreyButler.NVMforWindows --accept-package-agreements --accept-source-agreements"))

    if is_missing(checks, "node") or is_missing(checks, "npm"):
        if IS_WINDOWS:
            commands.append(("Node 24.14.1", build_nvm_node_command()))
        else:
            commands.append(("Node.js", label("ui.repair.node.manual", "请先使用系统包管理器安装 nodejs 和 npm")))
        will_have_node = True

    if is_missing(checks, "codex") and will_have_node:
        commands.append(("Codex CLI", f"{npm_command} install -g codex"))
    if is_missing(checks, "claude") and will_have_node:
        commands.append(("Claude Code", f"{npm_command} install -g @anthropic-ai/claude-code"))
    if is_missing(checks, "opencode") and will_have_node:
        commands.append(("OpenCode CLI", f"{npm_command} install -g opencode-ai"))

    return commands


def build_repair_command_models(checks: dict[str, CheckSnapshot], translate=None) -> list[RepairCommand]:
    return [
        RepairCommand(label=label, command=command, runnable=is_runnable_command(command))
        for label, command in build_repair_commands(checks, translate)
    ]
