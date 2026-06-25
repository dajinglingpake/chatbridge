from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Callable


Translate = Callable[[str], str]


HELP_MESSAGE_KEYS: tuple[str, ...] = (
    "bridge.help.title",
    "bridge.help.help",
    "bridge.help.status",
    "bridge.help.context",
    "bridge.help.new",
    "bridge.help.list",
    "bridge.help.sessions.page",
    "bridge.help.sessions.search",
    "bridge.help.sessions.delete",
    "bridge.help.sessions.clear_empty",
    "bridge.help.preview",
    "bridge.help.history",
    "bridge.help.export",
    "bridge.help.showfile",
    "bridge.help.sendfile",
    "bridge.help.events",
    "bridge.help.use",
    "bridge.help.rename",
    "bridge.help.delete",
    "bridge.help.cancel",
    "bridge.help.retry",
    "bridge.help.task",
    "bridge.help.last",
    "bridge.help.agent.current",
    "bridge.help.agent.list",
    "bridge.help.agent.commands",
    "bridge.help.agent.switch",
    "bridge.help.restart",
    "bridge.help.notify.current",
    "bridge.help.notify.switch",
    "bridge.help.backend.current",
    "bridge.help.backend.switch",
    "bridge.help.model",
    "bridge.help.model.switch",
    "bridge.help.model.reset",
    "bridge.help.project",
    "bridge.help.project.add",
    "bridge.help.project.remove",
    "bridge.help.project.list",
    "bridge.help.project.sessions",
    "bridge.help.project.switch",
    "bridge.help.project.reset",
    "bridge.help.clear",
    "bridge.help.close",
    "bridge.help.reset",
    "bridge.help.normal",
    "bridge.help.normal.detail",
    "bridge.help.escape",
)

QQ_HELP_MESSAGE_KEYS: tuple[str, ...] = (
    "bridge.help.title",
    "bridge.help.help",
    "bridge.help.status",
    "bridge.help.task",
    "bridge.help.last",
    "bridge.help.cancel",
    "bridge.help.retry",
    "bridge.help.normal",
    "bridge.help.normal.detail",
    "bridge.help.escape",
)


@dataclass(frozen=True)
class BridgeCommand:
    raw: str
    command: str
    args: str
    parts: tuple[str, ...]
    passthrough_prompt: str = ""

    @property
    def is_passthrough(self) -> bool:
        return self.raw.startswith("//")


def normalize_command_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    normalized = normalized.replace("\ufeff", "").replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    if not lines:
        return ""
    return lines[0]


def parse_bridge_command(text: str) -> BridgeCommand | None:
    raw = normalize_command_text(text)
    if not raw.startswith("/"):
        return None
    if raw.startswith("//"):
        passthrough = raw[1:].strip()
        parts = passthrough.split(maxsplit=2)
        command = parts[0].lower() if parts else ""
        args = passthrough[len(command) :].strip() if command else ""
        return BridgeCommand(raw=raw, command=command, args=args, parts=tuple(parts), passthrough_prompt=passthrough)
    parts = raw.split(maxsplit=2)
    command = parts[0].lower() if parts else ""
    args = raw[len(command) :].strip() if command else ""
    return BridgeCommand(raw=raw, command=command, args=args, parts=tuple(parts))


def render_bridge_help(translate: Translate, keys: tuple[str, ...] = HELP_MESSAGE_KEYS) -> str:
    blocks: list[str] = []
    previous_blank = False
    for key in keys:
        line = translate(key)
        if key == "bridge.help.normal":
            if blocks and not previous_blank:
                blocks.append("")
            previous_blank = True
        if line:
            blocks.append(line)
            previous_blank = False
    return "\n\n".join(line for line in blocks if line)
