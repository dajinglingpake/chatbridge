from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any

from bridge_config import BridgeConfig
from env_tools import collect_check_step, collect_lightweight_checks, get_full_check_sequence, get_full_check_step_label
from runtime_stack import (
    BRIDGE_CONVERSATIONS_PATH,
    BRIDGE_ERR_LOG,
    BRIDGE_OUT_LOG,
    BRIDGE_STATE_PATH,
    HUB_ERR_LOG,
    HUB_OUT_LOG,
    HUB_STATE_PATH,
    discover_external_agent_processes,
    get_runtime_snapshot,
    read_json,
)


@dataclass
class DashboardState:
    snapshot: Any
    bridge_config: BridgeConfig
    hub_state: dict[str, Any]
    bridge_state: dict[str, Any]
    bridge_conversations: dict[str, Any]
    checks: dict[str, Any]
    checks_in_progress: bool
    checks_progress_text: str
    active_account_id: str
    logs: dict[str, str]
    external_agent_processes: list[dict[str, Any]]


_CACHE_TTLS = {
    "checks": 30.0,
    "logs": 5.0,
    "external_agent_processes": 10.0,
}

_RUNTIME_CACHE: dict[str, tuple[float, Any]] = {}
_FULL_CHECK_PROGRESS_KEY = "checks:full:progress"


def _page_load_profile(page_key: str) -> dict[str, str | bool]:
    normalized = (page_key or "home").strip().lower()
    return {
        "checks_mode": "light" if normalized == "home" else ("full" if normalized in {"issues", "diagnostics"} else "none"),
        "logs": normalized == "diagnostics",
        "external_agent_processes": normalized == "diagnostics",
        "bridge_conversations": normalized == "sessions",
    }


def _read_cached(cache_key: str, loader, ttl_seconds: float) -> Any:
    now = time.monotonic()
    cached = _RUNTIME_CACHE.get(cache_key)
    if cached is not None:
        cached_at, payload = cached
        if now - cached_at <= ttl_seconds:
            return payload
    payload = loader()
    _RUNTIME_CACHE[cache_key] = (now, payload)
    return payload


def _get_progressive_full_checks(app_dir: Path, bridge_config: BridgeConfig) -> tuple[dict[str, Any], bool, str]:
    sequence = get_full_check_sequence()
    now = time.monotonic()
    ttl_seconds = _CACHE_TTLS["checks"]
    cached = _RUNTIME_CACHE.get(_FULL_CHECK_PROGRESS_KEY)
    if cached is None:
        state = {"results": {}, "next_index": 0, "updated_at": now}
    else:
        _, state = cached
        if now - float(state.get("updated_at", 0.0)) > ttl_seconds:
            state = {"results": {}, "next_index": 0, "updated_at": now}

    next_index = int(state.get("next_index", 0))
    results = dict(state.get("results") or {})
    if next_index < len(sequence):
        step_results = collect_check_step(sequence[next_index], app_dir, bridge_config)
        for result in step_results:
            results[result.key] = result
        next_index += 1

    state = {"results": results, "next_index": next_index, "updated_at": now}
    _RUNTIME_CACHE[_FULL_CHECK_PROGRESS_KEY] = (now, state)
    completed = next_index >= len(sequence)
    current_step_label = get_full_check_step_label(sequence[min(next_index, len(sequence) - 1)]) if not completed else "已完成"
    progress_text = f"环境检查进行中：{min(next_index, len(sequence))}/{len(sequence)}，当前步骤：{current_step_label}"
    return results, (not completed), progress_text


def tail_text(path: Path, max_lines: int = 80) -> str:
    if not path.exists():
        return "(empty)"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(unreadable)"
    return "\n".join(lines[-max_lines:]) if lines else "(empty)"


def load_dashboard_state(app_dir, page_key: str = "home") -> DashboardState:
    profile = _page_load_profile(page_key)
    snapshot = get_runtime_snapshot()
    bridge_config = BridgeConfig.load()
    hub_state = read_json(HUB_STATE_PATH)
    bridge_state = read_json(BRIDGE_STATE_PATH)
    bridge_conversations = read_json(BRIDGE_CONVERSATIONS_PATH) if profile["bridge_conversations"] else {}
    checks_mode = str(profile["checks_mode"])
    checks_in_progress = False
    checks_progress_text = ""
    if checks_mode == "full":
        checks, checks_in_progress, checks_progress_text = _get_progressive_full_checks(app_dir, bridge_config)
    elif checks_mode == "light":
        checks = _read_cached(
            "checks:light",
            lambda: {item.key: item for item in collect_lightweight_checks(app_dir, bridge_config)},
            _CACHE_TTLS["checks"],
        )
    else:
        checks = {}
    active_account_id = bridge_config.active_account_id
    logs = (
        _read_cached(
            "logs",
            lambda: {
                "hub_out": tail_text(HUB_OUT_LOG),
                "hub_err": tail_text(HUB_ERR_LOG),
                "bridge_out": tail_text(BRIDGE_OUT_LOG),
                "bridge_err": tail_text(BRIDGE_ERR_LOG),
            },
            _CACHE_TTLS["logs"],
        )
        if profile["logs"]
        else {}
    )
    return DashboardState(
        snapshot=snapshot,
        bridge_config=bridge_config,
        hub_state=hub_state,
        bridge_state=bridge_state,
        bridge_conversations=bridge_conversations,
        checks=checks,
        checks_in_progress=checks_in_progress,
        checks_progress_text=checks_progress_text,
        active_account_id=active_account_id,
        logs=logs,
        external_agent_processes=(
            _read_cached(
                "external_agent_processes",
                discover_external_agent_processes,
                _CACHE_TTLS["external_agent_processes"],
            )
            if profile["external_agent_processes"]
            else []
        ),
    )
