from __future__ import annotations

from collections.abc import Callable
from collections.abc import Iterable
from typing import Protocol

from core.bridge_command_catalog import parse_bridge_command


class BridgeCommandHandler(Protocol):
    def handle(self, sender_id: str, text: str): ...


class BridgeCommandRouter:
    def __init__(
        self,
        handlers: Iterable[BridgeCommandHandler],
        *,
        unknown_bridge_command_reply: Callable[[], str] | None = None,
    ) -> None:
        self.handlers = tuple(handlers)
        self.unknown_bridge_command_reply = unknown_bridge_command_reply

    def handle(self, sender_id: str, text: str) -> tuple[str, bool]:
        for handler in self.handlers:
            result = handler.handle(sender_id, text)
            if result.handled:
                return result.reply, True
        if self.unknown_bridge_command_reply is not None:
            parsed = parse_bridge_command(text)
            if parsed is not None and not parsed.is_passthrough:
                return self.unknown_bridge_command_reply(), True
        return "", False
