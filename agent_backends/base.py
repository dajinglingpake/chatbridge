from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class AgentLike(Protocol):
    id: str
    name: str
    workdir: str
    session_file: str
    backend: str
    model: str
    prompt_prefix: str


@dataclass(frozen=True)
class BackendContext:
    codex_command: str
    claude_command: str
    opencode_command: str
    session_dir: Path
    creationflags: int


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
