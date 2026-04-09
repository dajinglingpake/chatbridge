from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from agent_backends.base import AgentLike, BackendContext


def build_final_prompt(agent: AgentLike, prompt: str) -> str:
    return prompt if not agent.prompt_prefix else f"{agent.prompt_prefix}\n\n{prompt}"


def resolve_session_file(agent: AgentLike, session_name: str, session_dir: Path) -> Path:
    raw_name = (session_name or "").strip()
    if not raw_name:
        return Path(agent.session_file)
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in raw_name).strip("-_") or "default"
    return session_dir / f"{agent.id}__{safe}.txt"


def collect_text_fragments(value: Any) -> list[str]:
    if isinstance(value, list):
        fragments: list[str] = []
        for item in value:
            fragments.extend(collect_text_fragments(item))
        return fragments
    if not isinstance(value, dict):
        return []

    fragments: list[str] = []
    text_keys = {"text", "message", "content", "output", "response"}
    role = str(value.get("role") or "").lower()
    event_type = str(value.get("type") or value.get("event") or "").lower()
    for key, item in value.items():
        if isinstance(item, str) and key in text_keys:
            if role in {"assistant", ""} or "assistant" in event_type or "message" in event_type or "response" in event_type:
                fragments.append(item)
            continue
        if isinstance(item, (dict, list)):
            fragments.extend(collect_text_fragments(item))
    return fragments


def extract_session_id(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("session_id", "sessionId", "thread_id", "threadId"):
            raw = value.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
        session = value.get("session")
        if isinstance(session, dict):
            for key in ("id", "session_id", "sessionId"):
                raw = session.get(key)
                if isinstance(raw, str) and raw.strip():
                    return raw.strip()
        for item in value.values():
            found = extract_session_id(item)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = extract_session_id(item)
            if found:
                return found
    return ""


def extract_error_text(value: Any) -> str:
    if isinstance(value, dict):
        event_type = str(value.get("type") or value.get("event") or "").lower()
        if "error" in event_type:
            for key in ("message", "error", "content"):
                raw = value.get(key)
                if isinstance(raw, str) and raw.strip():
                    return raw.strip()
        for item in value.values():
            nested = extract_error_text(item)
            if nested:
                return nested
    if isinstance(value, list):
        for item in value:
            nested = extract_error_text(item)
            if nested:
                return nested
    return ""


def find_latest_opencode_session(workdir: Path, context: BackendContext) -> str:
    completed = subprocess.run(
        [context.opencode_command, "session", "list", "-n", "1", "--format", "json"],
        cwd=str(workdir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=context.creationflags,
        check=False,
        shell=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return ""
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return ""
    items = payload if isinstance(payload, list) else [payload]
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in ("id", "session_id", "sessionId"):
            raw = item.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    return ""
