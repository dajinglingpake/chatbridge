from __future__ import annotations

import json
import subprocess
import threading
import time
from typing import Any

from agent_hub import HubConfig


CODEX_MODEL_CATALOG_CACHE_SECONDS = 300.0
_CODEX_MODEL_CATALOG_CACHE: tuple[float, list[dict[str, Any]]] | None = None
_CODEX_MODEL_CATALOG_LOCK = threading.Lock()


def _copy_model_catalog(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            **entry,
            "reasoning_levels": list(entry.get("reasoning_levels") or []),
        }
        for entry in entries
    ]


def load_codex_model_catalog() -> list[dict[str, Any]]:
    command = HubConfig.load().codex_command
    completed = subprocess.run(
        [command, "debug", "models"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    payload = json.loads(completed.stdout or "{}")
    raw_models = payload.get("models") if isinstance(payload, dict) else []
    entries: list[dict[str, Any]] = []
    for item in raw_models or []:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("slug") or "").strip()
        visibility = str(item.get("visibility") or "").strip().lower()
        if not slug or visibility not in {"list", "default", "recommended"}:
            continue
        reasoning_levels = [
            str(level.get("effort") or "").strip()
            for level in (item.get("supported_reasoning_levels") or [])
            if isinstance(level, dict) and str(level.get("effort") or "").strip()
        ]
        entries.append(
            {
                "slug": slug,
                "display_name": str(item.get("display_name") or slug).strip() or slug,
                "description": str(item.get("description") or "").strip(),
                "default_reasoning": str(item.get("default_reasoning_level") or "").strip(),
                "reasoning_levels": reasoning_levels,
            }
        )
    return entries


def load_codex_model_catalog_cached(*, force: bool = False) -> list[dict[str, Any]]:
    global _CODEX_MODEL_CATALOG_CACHE
    now = time.monotonic()
    with _CODEX_MODEL_CATALOG_LOCK:
        if not force and _CODEX_MODEL_CATALOG_CACHE is not None:
            cached_at, cached_entries = _CODEX_MODEL_CATALOG_CACHE
            if now - cached_at <= CODEX_MODEL_CATALOG_CACHE_SECONDS:
                return _copy_model_catalog(cached_entries)
        entries = load_codex_model_catalog()
        _CODEX_MODEL_CATALOG_CACHE = (time.monotonic(), _copy_model_catalog(entries))
        return _copy_model_catalog(entries)


def display_reasoning_effort(effort: str) -> str:
    cleaned = str(effort or "").strip().lower()
    if not cleaned:
        return "-"
    if cleaned == "xhigh":
        return "XHigh"
    return cleaned.capitalize()


def display_model(model: str) -> str:
    return str(model or "").strip() or "-"
