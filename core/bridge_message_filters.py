from __future__ import annotations

from collections.abc import Callable, Sequence


def has_ignored_prefix(text: str, prefixes: Sequence[str]) -> bool:
    value = str(text or "")
    return any(value.startswith(prefix) for prefix in prefixes)


class BridgeDuplicateMessageFilter:
    def __init__(
        self,
        *,
        max_keys: int = 200,
        command_window_seconds: float = 2.0,
        fingerprint_ttl_seconds: float = 10.0,
        now: Callable[[], float],
    ) -> None:
        self.max_keys = max(1, int(max_keys))
        self.command_window_seconds = max(0.0, float(command_window_seconds))
        self.fingerprint_ttl_seconds = max(self.command_window_seconds, float(fingerprint_ttl_seconds))
        self.now = now
        self._keys: list[str] = []
        self._fingerprints: dict[str, float] = {}

    def is_duplicate(self, message_key: str, *, sender_id: str = "", text: str = "") -> bool:
        now_value = float(self.now())
        cleaned_key = str(message_key or "").strip()
        cleaned_text = str(text or "").strip()
        fingerprint = f"{str(sender_id or '').strip()}::{cleaned_text}" if cleaned_text.startswith("/") else ""
        if cleaned_key and cleaned_key in self._keys:
            return True
        recent_seen_at = self._fingerprints.get(fingerprint)
        if fingerprint.strip(":") and recent_seen_at is not None and now_value - recent_seen_at <= self.command_window_seconds:
            return True
        if cleaned_key:
            self._keys.append(cleaned_key)
            if len(self._keys) > self.max_keys:
                self._keys = self._keys[-self.max_keys :]
        if fingerprint:
            self._fingerprints[fingerprint] = now_value
        expired = [key for key, seen_at in self._fingerprints.items() if now_value - seen_at > self.fingerprint_ttl_seconds]
        for key in expired:
            self._fingerprints.pop(key, None)
        return False
