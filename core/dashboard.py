from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Callable, Iterable, TypeVar, cast

from bridge_config import BridgeConfig, normalize_backend
from core.accounts import account_conversation_path
from env_tools import collect_check_step, collect_lightweight_checks, get_full_check_sequence, get_full_check_step_label
from core.state_models import CheckSnapshot, ExternalAgentProcessState, HubStateSnapshot, RuntimeSnapshot, WeixinBridgeRuntimeState, WeixinConversationBinding
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
    snapshot: RuntimeSnapshot
    bridge_config: BridgeConfig
    hub_state: HubStateSnapshot
    bridge_state: WeixinBridgeRuntimeState
    bridge_conversations: dict[str, WeixinConversationBinding]
    checks: dict[str, CheckSnapshot]
    checks_in_progress: bool
    checks_progress_text: str
    active_account_id: str
    logs: dict[str, str]
    external_agent_processes: list[ExternalAgentProcessState]


@dataclass(frozen=True)
class PageLoadProfile:
    checks_mode: str = "none"
    logs: bool = False
    external_agent_processes: bool = False
    bridge_conversations: bool = False


@dataclass
class RuntimeCacheEntry:
    cached_at: float
    payload: object

    def is_fresh(self, *, now: float, ttl_seconds: float) -> bool:
        return now - self.cached_at <= ttl_seconds


@dataclass
class FullCheckProgressState:
    results: dict[str, CheckSnapshot]
    next_index: int
    updated_at: float

    @classmethod
    def create(cls, *, now: float) -> "FullCheckProgressState":
        return cls(results={}, next_index=0, updated_at=now)

    @classmethod
    def from_cached_payload(cls, raw: object, *, now: float) -> "FullCheckProgressState":
        if isinstance(raw, cls):
            return raw
        if not isinstance(raw, dict):
            return cls.create(now=now)
        results = raw.get("results")
        return cls(
            results=_coerce_check_map(results),
            next_index=_coerce_int(raw.get("next_index"), default=0),
            updated_at=_coerce_float(raw.get("updated_at"), default=now),
        )

    def is_expired(self, *, now: float, ttl_seconds: float) -> bool:
        return now - self.updated_at > ttl_seconds


_CACHE_TTLS = {
    "checks": 30.0,
    "logs": 5.0,
    "external_agent_processes": 10.0,
}

_RUNTIME_CACHE: dict[str, RuntimeCacheEntry] = {}
_FULL_CHECK_PROGRESS_KEY = "checks:full:progress"
CacheValueT = TypeVar("CacheValueT")


def _page_load_profile(page_key: str) -> PageLoadProfile:
    normalized = (page_key or "home").strip().lower()
    return PageLoadProfile(
        checks_mode="light" if normalized == "home" else ("full" if normalized in {"issues", "diagnostics"} else "none"),
        logs=normalized == "diagnostics",
        external_agent_processes=normalized == "diagnostics",
        bridge_conversations=normalized == "sessions",
    )


def _read_cached(cache_key: str, loader: Callable[[], CacheValueT], ttl_seconds: float) -> CacheValueT:
    now = time.monotonic()
    cached = _RUNTIME_CACHE.get(cache_key)
    if cached is not None and cached.is_fresh(now=now, ttl_seconds=ttl_seconds):
        return cast(CacheValueT, cached.payload)
    payload = loader()
    _RUNTIME_CACHE[cache_key] = RuntimeCacheEntry(cached_at=now, payload=payload)
    return payload


def _get_progressive_full_checks(app_dir: Path, bridge_config: BridgeConfig) -> tuple[dict[str, CheckSnapshot], bool, str]:
    sequence = get_full_check_sequence()
    if not sequence:
        return {}, False, "环境检查已完成"
    now = time.monotonic()
    ttl_seconds = _CACHE_TTLS["checks"]
    cached = _RUNTIME_CACHE.get(_FULL_CHECK_PROGRESS_KEY)
    state = FullCheckProgressState.from_cached_payload(cached.payload if cached is not None else None, now=now)
    if state.is_expired(now=now, ttl_seconds=ttl_seconds):
        state = FullCheckProgressState.create(now=now)

    next_index = state.next_index
    results = dict(state.results)
    if next_index < len(sequence):
        step_results = _index_checks(collect_check_step(sequence[next_index], app_dir, bridge_config))
        results.update(step_results)
        next_index += 1

    state = FullCheckProgressState(results=results, next_index=next_index, updated_at=now)
    _RUNTIME_CACHE[_FULL_CHECK_PROGRESS_KEY] = RuntimeCacheEntry(cached_at=now, payload=state)
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


def load_dashboard_state(app_dir: Path, page_key: str = "home") -> DashboardState:
    profile = _page_load_profile(page_key)
    snapshot = get_runtime_snapshot()
    bridge_config = BridgeConfig.load()
    hub_state = _read_hub_state(HUB_STATE_PATH, bridge_config)
    bridge_state = _read_bridge_state(BRIDGE_STATE_PATH)
    bridge_conversations = _read_bridge_conversations(BRIDGE_CONVERSATIONS_PATH, bridge_config) if profile.bridge_conversations else {}
    checks_mode = profile.checks_mode
    checks_in_progress = False
    checks_progress_text = ""
    if checks_mode == "full":
        checks, checks_in_progress, checks_progress_text = _get_progressive_full_checks(app_dir, bridge_config)
    elif checks_mode == "light":
        checks = _read_cached(
            "checks:light",
            lambda: _index_checks(collect_lightweight_checks(app_dir, bridge_config)),
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
        if profile.logs
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
            if profile.external_agent_processes
            else []
        ),
    )


def _coerce_int(value: object, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: object, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _read_hub_state(path: Path, bridge_config: BridgeConfig) -> HubStateSnapshot:
    return HubStateSnapshot.from_dict(
        read_json(path),
        default_backend=bridge_config.default_backend,
        now=_state_now(),
    )


def _read_bridge_state(path: Path) -> WeixinBridgeRuntimeState:
    return WeixinBridgeRuntimeState.from_dict(read_json(path))


def _read_bridge_conversations(path: Path, bridge_config: BridgeConfig) -> dict[str, WeixinConversationBinding]:
    active_path = account_conversation_path(path, bridge_config.active_account_id, bridge_config.account_file)
    payload = read_json(active_path)
    bindings: dict[str, WeixinConversationBinding] = {}
    for sender_id, raw_binding in payload.items():
        cleaned_sender_id = str(sender_id or "").strip()
        if not cleaned_sender_id:
            continue
        bindings[cleaned_sender_id] = WeixinConversationBinding.from_dict(
            raw_binding,
            default_backend=bridge_config.default_backend,
            now=_state_now(),
            normalize_backend=normalize_backend,
        )
    return bindings


def _index_checks(results: Iterable[object]) -> dict[str, CheckSnapshot]:
    checks: dict[str, CheckSnapshot] = {}
    for item in results:
        check = CheckSnapshot.from_result(item)
        if check is None:
            continue
        checks[check.key] = check
    return checks


def _coerce_check_map(raw: object) -> dict[str, CheckSnapshot]:
    if not isinstance(raw, dict):
        return {}
    checks: dict[str, CheckSnapshot] = {}
    for key, value in raw.items():
        check = CheckSnapshot.from_result(value)
        if check is None:
            check = CheckSnapshot.from_dict(value)
        if check is None:
            continue
        checks[str(key or check.key)] = check
    return checks


def _state_now() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")
