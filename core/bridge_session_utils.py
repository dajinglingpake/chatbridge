from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol


class _SessionBinding(Protocol):
    current_session: str
    last_regular_session: str
    sessions: dict[str, object]


def sanitize_session_name(requested: str, *, fallback: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in requested).strip("-_") or fallback


def sanitize_project_name(requested: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in requested).strip("-_")


def split_named_path_args(raw: str) -> tuple[str, str]:
    parts = str(raw or "").strip().split(maxsplit=1)
    if len(parts) < 2:
        return (parts[0], "") if parts else ("", "")
    return parts[0].strip(), parts[1].strip()


def allocate_session_name(binding: _SessionBinding, requested: str) -> str:
    base = sanitize_session_name(requested, fallback="session")
    if base not in binding.sessions:
        return base
    index = 2
    while f"{base}-{index}" in binding.sessions:
        index += 1
    return f"{base}-{index}"


def resolve_fallback_session_target(binding: _SessionBinding) -> str:
    if binding.last_regular_session and binding.last_regular_session in binding.sessions:
        return binding.last_regular_session
    return next(iter(binding.sessions.keys()), "")


def create_session_meta(
    meta_type: Callable[..., Any],
    *,
    backend: Any,
    default_backend: str,
    now: str,
    normalize_backend: Callable[[str], str],
    workdir: str = "",
    model: str = "",
    reasoning_effort: str = "",
    permission_mode: str = "",
) -> Any:
    return meta_type(
        backend=normalize_backend(str(backend or default_backend)),
        created_at=now,
        updated_at=now,
        workdir=workdir.strip(),
        model=model.strip(),
        reasoning_effort=reasoning_effort.strip(),
        permission_mode=permission_mode.strip(),
    )
