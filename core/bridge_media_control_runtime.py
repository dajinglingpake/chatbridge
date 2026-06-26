from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.bridge_command_catalog import parse_bridge_command
from core.bridge_runtime import BridgeCommandResult


class BridgeMediaControlRuntime:
    def __init__(
        self,
        *,
        translate: Callable[..., str],
        resolve_file: Callable[[str], Path],
        send_file: Callable[[dict[str, Any], Path], None],
    ) -> None:
        self.translate = translate
        self.resolve_file = resolve_file
        self.send_file = send_file

    def handle(self, reply_target: dict[str, Any], text: str) -> BridgeCommandResult:
        parsed = parse_bridge_command(text)
        if parsed is None or parsed.is_passthrough or parsed.command != "/sendfile":
            return BridgeCommandResult(False)
        raw_path = parsed.parts[1].strip() if len(parsed.parts) >= 2 else ""
        if not raw_path:
            return BridgeCommandResult(True, self.translate("bridge.sendfile.usage"))
        try:
            file_path = self.resolve_file(raw_path)
            self.send_file(reply_target, file_path)
        except Exception as exc:  # noqa: BLE001
            return BridgeCommandResult(True, self.translate("bridge.sendfile.failed", path=raw_path, error=str(exc)))
        return BridgeCommandResult(True)
