from __future__ import annotations

import hashlib
import time
from contextlib import contextmanager
from pathlib import Path

from core.runtime_paths import STATE_DIR

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


LOCK_DIR = STATE_DIR / "weixin_send_locks"


def _lock_path_for_sender(sender_id: str) -> Path:
    cleaned = str(sender_id or "").strip() or "unknown"
    digest = hashlib.sha1(cleaned.encode("utf-8")).hexdigest()[:16]
    return LOCK_DIR / f"{digest}.lock"


@contextmanager
def sender_send_lock(sender_id: str, *, timeout_seconds: float = 10.0):
    if fcntl is None:
        yield
        return
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path_for_sender(sender_id)
    with lock_path.open("a+", encoding="utf-8") as handle:
        deadline = time.monotonic() + max(0.1, timeout_seconds)
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    break
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
