from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.bridge_runtime import IncomingBridgeMessage


class BridgeConversationRuntime:
    def __init__(
        self,
        *,
        ensure_conversation: Callable[[str], Any],
        default_backend: Callable[[], str],
        now: Callable[[], str],
        normalize_backend: Callable[[str], str],
    ) -> None:
        self.ensure_conversation = ensure_conversation
        self.default_backend = default_backend
        self.now = now
        self.normalize_backend = normalize_backend

    def resolve_session(self, message: IncomingBridgeMessage) -> dict[str, Any]:
        binding = self.ensure_conversation(message.sender_id)
        session_name, session_meta = binding.get_current_session(
            default_backend=self.default_backend(),
            now=self.now(),
            normalize_backend=self.normalize_backend,
        )
        return {"binding": binding, "session_name": session_name, "session_meta": session_meta}
