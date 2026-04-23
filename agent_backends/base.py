from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


class AgentLike(Protocol):
    id: str
    name: str
    workdir: str
    session_file: str
    backend: str
    model: str
    prompt_prefix: str


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    command: str
    args: list[str]


@dataclass(frozen=True)
class BackendContext:
    codex_command: str
    claude_command: str
    opencode_command: str
    session_dir: Path
    creationflags: int
    start_new_session: bool = False
    on_process_started: Callable[[int], None] | None = None
    on_progress: Callable[[str], None] | None = None
    mcp_server: McpServerConfig | None = None
    reasoning_effort: str = ""
    permission_mode: str = ""


class AgentBackend(Protocol):
    key: str

    def invoke(
        self,
        agent: AgentLike,
        prompt: str,
        session_name: str,
        context: BackendContext,
    ) -> dict[str, str]:
        ...
