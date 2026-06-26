from __future__ import annotations

import json
import subprocess
from typing import Any

from agent_hub import HubConfig


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
                "priority": int(item.get("priority") or 0),
            }
        )
    entries.sort(key=lambda entry: (-int(entry.get("priority") or 0), str(entry.get("slug") or "")))
    for entry in entries:
        entry.pop("priority", None)
    return entries


def display_reasoning_effort(effort: str) -> str:
    cleaned = str(effort or "").strip().lower()
    if not cleaned:
        return "-"
    if cleaned == "xhigh":
        return "Extra high"
    return cleaned.title()


def display_model(model: str) -> str:
    return str(model or "").strip() or "-"
