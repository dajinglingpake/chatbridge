from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

try:
    import psutil
except ImportError:  # pragma: no cover - optional dependency
    psutil = None


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
    if normalized in {"claude", "claude.cmd"}:
        return "claude.cmd" if IS_WINDOWS else "claude"
    if normalized in {"opencode", "opencode.cmd"}:
        return "opencode.cmd" if IS_WINDOWS else "opencode"
    if name == "npm":
        return "npm.cmd" if IS_WINDOWS else "npm"
    if name == "codex":
        return "codex.cmd" if IS_WINDOWS else "codex"
    if name == "claude":
        return "claude.cmd" if IS_WINDOWS else "claude"
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


def terminate_process_tree(pid: int) -> None:
    if pid <= 0:
        return
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
        try:
            proc = psutil.Process(pid)
        except (psutil.Error, ProcessLookupError):
            return
        children = proc.children(recursive=True)
        for child in children:
            try:
                child.terminate()
            except psutil.Error:
                pass
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except (psutil.Error, TimeoutError):
            try:
                proc.kill()
            except psutil.Error:
                pass
        for child in children:
            try:
                child.wait(timeout=1)
            except psutil.Error:
                try:
                    child.kill()
                except psutil.Error:
                    pass
        return

    try:
        os.killpg(pid, 15)
    except OSError:
        try:
            os.kill(pid, 15)
        except OSError:
            return
