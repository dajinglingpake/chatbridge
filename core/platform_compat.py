from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0


def creationflags() -> int:
    return CREATE_NO_WINDOW if IS_WINDOWS else 0


def default_command(name: str) -> str:
    normalized = name.strip()
    if normalized in {"npm", "npm.cmd"}:
        return "npm.cmd" if IS_WINDOWS else "npm"
    if normalized in {"codex", "codex.cmd"}:
        return "codex.cmd" if IS_WINDOWS else "codex"
    if normalized in {"opencode", "opencode.cmd"}:
        return "opencode.cmd" if IS_WINDOWS else "opencode"
    if name == "npm":
        return "npm.cmd" if IS_WINDOWS else "npm"
    if name == "codex":
        return "codex.cmd" if IS_WINDOWS else "codex"
    if name == "opencode":
        return "opencode.cmd" if IS_WINDOWS else "opencode"
    return name


def resolve_command(name: str) -> str:
    preferred = default_command(name)
    if shutil.which(preferred):
        return preferred
    fallback = name
    if fallback != preferred and shutil.which(fallback):
        return fallback
    return preferred


def command_candidates(name: str) -> list[str]:
    preferred = default_command(name)
    if preferred == name:
        return [name]
    return [preferred, name]


def executable_exists(command: str) -> bool:
    return shutil.which(command) is not None


def shell_command(command: str) -> list[str]:
    if IS_WINDOWS:
        return ["cmd", "/c", command]
    shell = os.environ.get("SHELL") or "/bin/bash"
    shell_path = Path(shell)
    if shell_path.exists():
        return [str(shell_path), "-lc", command]
    return ["/bin/sh", "-lc", command]
