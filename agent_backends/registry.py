from __future__ import annotations

from agent_backends.base import AgentBackend
from agent_backends.claude_backend import ClaudeBackend
from agent_backends.codex_backend import CodexBackend
from agent_backends.opencode_backend import OpenCodeBackend

DEFAULT_BACKEND_KEY = "codex"


def build_backend_registry() -> dict[str, AgentBackend]:
    backends: list[AgentBackend] = [
        CodexBackend(),
        ClaudeBackend(),
        OpenCodeBackend(),
    ]
    return {backend.key: backend for backend in backends}


def supported_backend_keys() -> tuple[str, ...]:
    return tuple(build_backend_registry().keys())


def supported_backend_options(include_default: bool = False) -> dict[str, str]:
    options = {key: key for key in supported_backend_keys()}
    if include_default:
        return {"": "跟随 Agent 默认配置", **options}
    return options
