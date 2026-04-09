from __future__ import annotations

import json
import locale
import os
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parent
LOCALES_DIR = APP_DIR / "locales"
DEFAULT_LANGUAGE = "zh-CN"
SUPPORTED_LANGUAGES = {"zh-CN", "en-US"}


class Localizer:
    def __init__(self, language: str = "") -> None:
        self.language = resolve_language(language)
        self.messages = load_messages(self.language)

    def translate(self, key: str, **kwargs: Any) -> str:
        template = self.messages.get(key) or key
        return template.format(**kwargs).replace("\\n", "\n")


def resolve_language(preferred: str = "") -> str:
    candidates = [
        normalize_language(preferred),
        normalize_language(os.environ.get("CHATBRIDGE_LANG") or ""),
        normalize_language(locale.getdefaultlocale()[0] or ""),
    ]
    for candidate in candidates:
        if candidate:
            return candidate
    return DEFAULT_LANGUAGE


def normalize_language(value: str) -> str:
    raw = (value or "").strip()
    if not raw or raw.lower() == "auto":
        return ""
    lowered = raw.replace("_", "-").lower()
    if lowered.startswith("zh"):
        return "zh-CN"
    if lowered.startswith("en"):
        return "en-US"
    return raw if raw in SUPPORTED_LANGUAGES else ""


def load_messages(language: str) -> dict[str, str]:
    resolved = resolve_language(language)
    primary_path = LOCALES_DIR / f"{resolved}.json"
    default_path = LOCALES_DIR / f"{DEFAULT_LANGUAGE}.json"
    messages = _read_locale_file(default_path)
    if primary_path != default_path:
        messages.update(_read_locale_file(primary_path))
    return messages


def _read_locale_file(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return {str(key): str(value) for key, value in data.items()}
