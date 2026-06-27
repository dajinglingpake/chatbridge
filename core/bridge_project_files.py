from __future__ import annotations

from pathlib import Path
from typing import Callable

SHOWFILE_PREVIEW_LIMIT = 3200
SHOWFILE_ALLOWED_EXTENSIONS = frozenset(
    {
        ".bat",
        ".cmd",
        ".css",
        ".html",
        ".js",
        ".json",
        ".md",
        ".ps1",
        ".py",
        ".sh",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".yaml",
        ".yml",
    }
)
SENDMEDIA_IMAGE_EXTENSIONS = frozenset({".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"})
SHOWFILE_BLOCKED_PATH_PARTS = frozenset({".git", ".runtime", ".venv", "__pycache__", "accounts", "sessions"})


def is_blocked_share_path(relative_path: Path) -> bool:
    parts = relative_path.parts
    if len(parts) >= 2 and parts[0] == ".runtime" and parts[1] == "exports":
        return False
    return any(part in SHOWFILE_BLOCKED_PATH_PARTS for part in parts)


def resolve_shareable_project_file(app_dir: Path, raw_path: str) -> Path:
    cleaned_path = raw_path.strip()
    if not cleaned_path:
        raise ValueError("path is required")
    project_root = app_dir.resolve()
    candidate = Path(cleaned_path).expanduser()
    if not candidate.is_absolute():
        candidate = project_root / candidate
    resolved = candidate.resolve()
    try:
        relative_path = resolved.relative_to(project_root)
    except ValueError as exc:
        raise ValueError(f"path is outside project: {cleaned_path}") from exc
    if is_blocked_share_path(relative_path):
        raise ValueError(f"path is blocked: {relative_path}")
    if not resolved.exists() or not resolved.is_file():
        raise FileNotFoundError(str(relative_path))
    return resolved


def render_project_file_preview(app_dir: Path, raw_path: str, *, translate: Callable[..., str]) -> str:
    cleaned_path = raw_path.strip()
    if not cleaned_path:
        return translate("bridge.showfile.usage")
    project_root = app_dir.resolve()
    candidate = Path(cleaned_path).expanduser()
    if not candidate.is_absolute():
        candidate = project_root / candidate
    try:
        resolved = candidate.resolve()
        relative_path = resolved.relative_to(project_root)
    except ValueError:
        return translate("bridge.showfile.denied", path=cleaned_path)
    if is_blocked_share_path(relative_path):
        return translate("bridge.showfile.denied", path=str(relative_path))
    if not resolved.exists() or not resolved.is_file():
        return translate("bridge.showfile.not_found", path=str(relative_path))
    suffix = resolved.suffix.lower()
    if suffix not in SHOWFILE_ALLOWED_EXTENSIONS:
        return translate("bridge.showfile.unsupported", path=str(relative_path), suffix=suffix or "-")
    content = resolved.read_text(encoding="utf-8", errors="replace")
    truncated = len(content) > SHOWFILE_PREVIEW_LIMIT
    preview = content[:SHOWFILE_PREVIEW_LIMIT].rstrip()
    if truncated:
        preview = f"{preview}\n\n...（内容过长，已截断）"
    return translate(
        "bridge.showfile.content",
        path=str(relative_path),
        size=resolved.stat().st_size,
        content=preview or "(empty)",
    )
